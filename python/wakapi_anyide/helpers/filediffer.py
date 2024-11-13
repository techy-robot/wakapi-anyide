import difflib
import logging
import re
import random
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256

from aiofiles import open
from aiofiles.ospath import getsize

from wakapi_anyide.models.environment import Environment
from wakapi_anyide.watchers.types import Event

logger = logging.getLogger(__name__)

@dataclass
class File:
    path: str
    linecount: int
    size: int
    checksum: str
    binary: bool
    body: bytes
    

    def too_large(self, max_size: int=65536):
        return self.size > max_size
    
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
    async def read(cls, path: str, max_size: int=65536):
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
            
            # TODO: Improve efficiency by reading the file only once, not 3 times for content, checksum, and lines.
            
            filebytes: bytes = b""
            binary = False
            
            # if the file is small enough, read it all into memory
            if size <= max_size:
                filebytes: bytes = await file.read()
            
            # We can't be sure that the file will be read above, so we need to check if it is binary separately and calculate the line count
            try:
                # test the first 512 bytes to see if it can be decoded as text
                await file.seek(0)
                filedecoded = await file.read(512)
                filedecoded = filedecoded.decode()
                
                # Read line count without loading the wholefile into memory
                line_count = await cls._count(cls, file) 
                            
            except UnicodeDecodeError:
                line_count = (size) / 100 # Read file size in bytes instead of line count. Estimate 100 bytes a line
                binary = True 
            
            # Calculate checksum without loading the wholefile into memory
            checksum = await cls.calculate_checksum(file)
              
                
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


def human_to_bytes(size: str) -> int:
    for i, suffix in enumerate(["KiB", "MiB", "GiB", "TiB"]):
        if size.endswith(suffix):
            
            # Splitting text and number in string
            res = re.findall(r'(\d+)(\w+?)', size)[0]
            number = res[0]
            return int(number) * (1024**(i+1))
    raise ValueError("Invalid size format")


def process_file_change(new_file: File, old_file: File, time: float, env: Environment) -> Event | None:
    filename = new_file.path
    file_extension = Path(new_file.path).suffix
    max_size = human_to_bytes(env.project.files.large_file_threshold)
    
    if new_file.too_large(max_size) or old_file.too_large(max_size):
        diff = new_file.linecount - old_file.linecount
        lines_added = max(0, diff)
        lines_removed = -min(0, diff)
        # rough estimate, if the file is different but total line count didn't change, fudge some numbers
        if diff == 0 and new_file.checksum != old_file.checksum:
            lines_added = random.randint(0, 20)
            lines_removed = random.randint(0, 20)
            
        return Event(
            filename=filename,
            file_extension=file_extension,
            cursor=(0, 0),
            lines_added=lines_added,
            lines_removed=lines_removed,
            lines=new_file.linecount,
            time=time
        )
    
    # Both files are text files
    if not new_file.binary and not old_file.binary:
        new_file_str = new_file.body.decode()
        old_file_str = old_file.body.decode()
        last_index = 0
        # finding rough cursor postion
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
        # finding changed lines
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
            lines=new_file.linecount,
            time=time
        )
    else:  # One of the files is binary
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
            lines=new_file.linecount,
            time=time
        )
