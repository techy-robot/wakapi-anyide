import difflib
import logging
from dataclasses import dataclass
from pathlib import Path

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
    
    @property
    def too_large(self, env_max_size: int):
        return self.size > env_max_size
    
    @classmethod
    async def read(cls, path: str):
        size = await getsize(path)
        
        #  Reads the file contents and returns the important metadata on it, but no content
        
        async with open(path, 'rb') as file:
            line_count = 0
            size = await getsize(path)
            
            # TODO: Check if binary too large according to user settings, and skip reading and calculate. 
            filebytes: bytes = await file.read()
            checksum = sha256(filebytes).hexdigest()
            binary = False
            """ def _count_generator(reader):
                b = reader(1024 * 1024)
                while b:
                    yield b
                    b = reader(1024 * 1024) """
            try:
                # we might want to support any encoding type in the future. This will suffice though just to detect an error
                
                # Read line count without loading the file into memory
                # c_generator = _count_generator(file.raw.read)
                # count each \n
                # line = sum(buffer.count(b'\n') for buffer in c_generator) +1
                
                                
                filedecoded = filebytes.decode()
                filelines = filedecoded.splitlines()
                line_count = len(filelines)
                
            except UnicodeDecodeError:
                line_count = (size) / 100 # Read file size in bytes instead of line count. Estimate 100 bytes a line
                binary = True
                
            return cls(
                path,
                line_count,
                size,
                checksum,
                binary
            )
    
    @classmethod
    def empty(cls, path: str):
        return cls(
            path,
            0,
            0,
            "",
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
    
    if new_file.too_large or old_file.too_large:
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
