import difflib
import logging
import random
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from aiofiles import open
from aiofiles.ospath import getsize
from wakapi_anyide.models.environment import Environment
from wakapi_anyide.watchers.types import Event

logger = logging.getLogger(__name__)


@dataclass
class FileMetadata:
    path: str
    linecount: int
    checksum: str
    binary: bool
    
    @classmethod
    async def read(cls, path: str):

        #  Reads the file contents and returns the important metadata on it, but no content
        
        async with open(path, 'rb') as file: # Problem right here: Its reading in binary mode
            line = 0
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
                line = len(filelines)
                
            except UnicodeDecodeError:
                line = (await getsize(path)) / 100 # Read file size in bytes instead of line count. Estimate 100 bytes a line
                binary = True
                
            return cls(
                path,
                line,
                checksum,
                binary
            )
    @classmethod
    def empty(cls, path: str):
        return cls(
            path,
            0,
            "",
            False
        )


def process_file_change(new_file: FileMetadata, old_file: FileMetadata, time: float, env: Environment) -> Event | None:
    filename = new_file.path
    file_extension = Path(new_file.path).suffix

    if (new_file.binary or old_file.binary):   
        if env.project.files.exclude_binary_files:
            logger.info(f"Ignored file {filename}")
            return
        file_extension=f"{filename}#wakapi-anyide-binaryfile" # if it is a binary file, add a flag
        
    lines_added = 0
    lines_removed = 0
    
    if new_file.checksum != old_file.checksum: # Compare checksums.If the file is modified, generate a baseline random line diff
        lines_added = random.randint(0, 20)
        lines_removed = random.randint(0, 20)
    
    # basic diff of either the linecount, or the byte count / 100 of the file 
    diff = new_file.linecount - old_file.linecount
    
    if diff != 0:# override the checksum random line diff if the file line count is actually different
        lines_added = max(0, diff)
        lines_removed = -min(0, diff)

    return Event(
        filename=filename,
        file_extension=file_extension,
        checksum=new_file.checksum,
        lines_added=lines_added,
        lines_removed=lines_removed,
        lines=new_file.linecount,
        time=time
    )
