import difflib
import logging

from wakapi_anyide.models.environment import Environment
from wakapi_anyide.watchers.types import Event

logger = logging.getLogger(__name__)


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


def process_file_change(filename: str, new_file: bytes, old_file: bytes, time: float, env: Environment) -> Event | None:
    
    file_extension = Path(filename).suffix
    
    if len(new_file) > 2**16 or len(old_file) > 2**16:
        diff = len(new_file) - len(old_file)
        lines_added = max(0, diff)
        lines_removed = -min(0, diff)
        
        return Event(
            filename=filename,
            file_extension=file_extension,
            cursor=(0, 0),
            lines_added=lines_added,
            lines_removed=lines_removed,
            lines=len(new_file),
            time=time
        )
    
    try:
        new_file_str = new_file.decode()
        old_file_str = old_file.decode()

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
        for op in difflib.SequenceMatcher(a=old_file, b=new_file, autojunk=False).get_opcodes():
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
            file_extension=f"#wakapi-anyide-binaryfile", # custom handling for binary files
            cursor=(1, last_index),
            lines_added=added_lines,
            lines_removed=deleted_lines,
            lines=len(new_file),
            time=time
        )
