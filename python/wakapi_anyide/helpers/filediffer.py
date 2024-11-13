import difflib
import logging
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256

from aiofiles import open
from aiofiles.ospath import getsize

from wakapi_anyide.models.environment import Environment
from wakapi_anyide.watchers.types import Event

logger = logging.getLogger(__name__)

SIZE_MAX = 2**16


@dataclass
class File:
    path: str
    linecount: int
    size: int
    checksum: str
    binary: bool
    body: bytes
    

    def too_large(self, env_max_size: int):
        return self.size > env_max_size
    
    # reads through the file as chunks, to be iterated over
    async def _block_generator(filechunks, size=256*256):
        """
        Reads the file in chunks of size `size`, and yields each chunk.
        Use this generator to iterate over the file in chunks without loading the whole file into memory.
        """
        
        while True:
            b = await filechunks.read(size)
            if not b:
                break
            yield b
    async def _count(cls, file):
        """
        Count the number of newlines in a file, without loading the whole file into memory.
        """
        
        count = 0
        await file.seek(0)# reset file after potentially reading the first time
        # count each \n
        async for bl in cls._block_generator(file):
            count += bl.count(b"\n")
        return count
    
    async def calculate_checksum(file):
        """
        Reads the file in chunks and calculates its SHA256 checksum.

        Args:
            file: An open file object (in binary mode) to read the checksum from.

        Returns:
            A hex string representing the checksum of the file content.
        """
        # At some point I might want to support calling this function in the main program.
        
        await file.seek(0)# reset file after potentially reading the first time
        file_hash = sha256()
        while chunk := await file.read(8192):
            file_hash.update(chunk)
        return file_hash.hexdigest()
    
    @classmethod
    async def read(cls, path: str):
        """
        Reads a file from the given path and returns a File object encapsulating its metadata.

        The function opens the file in binary mode and calculates the following metadata:
        - path: The file path.
        - linecount: The number of lines in the file, or an estimated count if the file is binary.
        - size: The size of the file in bytes.
        - checksum: The SHA256 checksum of the file content.
        - binary: A boolean indicating whether the file is binary.
        - body: The raw bytes content of the file.

        Note:
            The function reads the file multiple times for content, checksum, and line count,
            which may affect performance for large files. This is a known issue to be addressed.
            If the file cannot be decoded as a text file, it is treated as a binary file, and
            an estimated line count is calculated based on file size.

        Args:
            path (str): The path to the file to be read.

        Returns:
            File: An instance of the File class containing the metadata of the file.
        """
        size = await getsize(path)
        
        #  Reads the file contents and returns the important metadata on it, but no content
        
        async with open(path, 'rb') as file:
            line_count = 0
            size = await getsize(path)
            
            # TODO: Check if binary too large according to user settings, and skip reading and calculate. 
            
            # TODO: Improve efficiency by reading the file only once, not 3 times for content, checksum, and lines.
            
            filebytes: bytes = await file.read()
            checksum = await cls.calculate_checksum(file)
            print(f"checksum: {checksum}")
            binary = False
            
            # Splits out file blocks to save memory as it counts lines

            try:
                # we might want to support any encoding type in the future. This will suffice though just to detect an error
                
                filedecoded = filebytes.decode()
                               
                # Read line count without loading the wholefile into memory
                line_count = await cls._count(cls, file)        
                print(f"line count: {line_count}")
                
            except UnicodeDecodeError:
                line_count = (size) / 100 # Read file size in bytes instead of line count. Estimate 100 bytes a line
                binary = True
                
            return cls(
                path,
                line_count,
                size,
                checksum,
                binary,
                filebytes
            )
    
    @classmethod
    def empty(cls, path: str):
        """
        Create a File object that represents an empty file.
        """
        return cls(
            path,
            0,
            0,
            "",
            False,
            b""
        )


def index_to_linecol(file: str, index: int):
    line = 0
    col = 0

    for character in file[:index]:
        if character == '\n':
            line += 1
            col = 0
        else:
            col += 1

    return line + 1, col + 1


def process_file_change(new_file: File, old_file: File, time: float, env: Environment) -> Event | None:
    filename = new_file.path
    file_extension = Path(new_file.path).suffix
    
    if new_file.too_large(SIZE_MAX) or old_file.too_large(SIZE_MAX):
        diff = new_file.size - old_file.size
        lines_added = max(0, diff)
        lines_removed = -min(0, diff)
        
        return Event(
            filename=f"{filename}#wakapi-anyide-toolarge",
            file_extension=file_extension,
            cursor=(0, 0),
            lines_added=lines_added,
            lines_removed=lines_removed,
            lines=new_file.size,
            time=time
        )
    
    try:
        new_file_str = new_file.body.decode()
        old_file_str = old_file.body.decode()

        last_index = 0
        for op in difflib.SequenceMatcher(a=old_file_str, b=new_file_str, autojunk=False).get_opcodes():
            match op:
                case ('replace', _, _, _, j2):
                    last_index = max(last_index, j2)
                case ('delete', i1, _, _, _):
                    last_index = max(last_index, i1)
                case ('insert', i1, _, j1, j2):
                    last_index = max(last_index, j2)
                case ('equal', _, _, _, _):
                    pass
                case _:
                    raise Exception(f"Unknown opcode {op}")

        new_file_lines = new_file_str.splitlines()
        added_lines = 0
        deleted_lines = 0
        for op in difflib.SequenceMatcher(a=old_file_str.splitlines(), b=new_file_lines, autojunk=False).get_opcodes():
            match op:
                case ('replace', i1, i2, j1, j2):
                    added_lines += j2 - j1
                    deleted_lines += i2 - i1
                case ('delete', i1, i2, _, _):
                    deleted_lines += i2 - i1
                case ('insert', _, _, j1, j2):
                    added_lines += j2 - j1
                case ('equal', _, _, _, _):
                    pass
                case _:
                    raise Exception(f"Unknown opcode {op}")

        line, col = index_to_linecol(new_file_str, last_index)

        return Event(
            filename=filename,
            file_extension=file_extension,
            cursor=(line, col),
            lines_added=added_lines,
            lines_removed=deleted_lines,
            lines=len(new_file_lines),
            time=time
        )
    except UnicodeDecodeError:
        if env.project.files.exclude_binary_files:
            logger.info(f"Ignored file {filename}")
            return

        added_lines = 0
        deleted_lines = 0
        last_index = 0
        for op in difflib.SequenceMatcher(a=old_file.body, b=new_file.body, autojunk=False).get_opcodes():
            match op:
                case ('replace', i1, i2, j1, j2):
                    added_lines += j2 - j1
                    deleted_lines += i2 - i1
                    last_index = max(last_index, j2)
                case ('delete', i1, i2, _, _):
                    deleted_lines += i2 - i1
                    last_index = max(last_index, i1)
                case ('insert', _, _, j1, j2):
                    added_lines += j2 - j1
                    last_index = max(last_index, j2)
                case ('equal', _, _, _, _):
                    pass
                case _:
                    raise Exception(f"Unknown opcode {op}")

        return Event(
            filename=filename,
            file_extension=f"{filename}#wakapi-anyide-binaryfile", # custom handling for binary files
            cursor=(1, last_index),
            lines_added=added_lines,
            lines_removed=deleted_lines,
            lines=len(new_file.body),
            time=time
        )
