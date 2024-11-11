import asyncio
import logging
import time
from json import dumps
from pathlib import Path
from typing import Annotated
from typing import TypeAlias

import typer
from wakapi_anyide import __version__
from wakapi_anyide.models.config import WakatimeConfig
from wakapi_anyide.models.environment import Environment
from wakapi_anyide.models.project import Project
from wakapi_anyide.runner import ConfigInvalidatedException
from wakapi_anyide.runner import run

logger = logging.getLogger(__name__)


DEFAULT_IGNOREFILES = [".gitignore"]
TEMPLATE = """
# https://github.com/iamawatermelo/wakapi-anyide v{version}

[meta]
version = 1

[files]
include = {include}  # files to include in tracking
exclude = {exclude}  # files to exclude in tracking
exclude_files = {exclude_files}  # files whose contents will be used to exclude other files from tracking
exclude_binary_files = true  # whether to ignore binary files

[project]
name = "{name}"  # your project name
"""

app = typer.Typer(
    pretty_exceptions_enable=False  # they don't report asyncio taskgroup exceptions correctly
)


def setup_logging(is_verbose: bool):
    try:
        from rich.logging import RichHandler
        logging.basicConfig(
            level="DEBUG" if is_verbose else "INFO",
            format="[bold magenta]{module}[/bold magenta][bright_black]:{funcName}@{lineno}[/bright_black]  {message}",
            style='{',
            datefmt="[%X]",
            handlers=[RichHandler(rich_tracebacks=True, markup=True, show_time=False, show_path=False)]
        )
    except ImportError:
        logging.basicConfig(
            level="DEBUG" if is_verbose else "INFO",
            format="{levelname} {filename}:{funcName}@{lineno} {message}",
            style='{',
            datefmt="[%X]"
        )
        logger.warning("Rich is not available, using basic logging (pip install rich?)")


Verbose: TypeAlias = Annotated[bool, typer.Option("--verbose", callback=setup_logging)]


def start(is_test):
    while True:
        try:
            asyncio.run(run(Environment(
                is_test_only=is_test,
                config=WakatimeConfig(),  # type: ignore
                project=Project()  # type: ignore
            )))
        except ConfigInvalidatedException:
            logger.warning(f"Detected config change, restarting in 1s")
            time.sleep(1)
            continue
        except KeyboardInterrupt:
            break


@app.command()
def test(verbose: Verbose = False):
    start(True)


@app.command()
def track(verbose: Verbose = False):
    start(False)


@app.command()
def setup():
    output = Path("wak.toml")
    if output.exists():
        raise Exception("a wak.toml already exists in this directory")
        
    project_name = typer.prompt("What's your project name?", default=Path("./").absolute().name)
    
    included_paths = list()
    if typer.confirm("Would you like to watch all files in the directory?", default=True):
        included_paths.append("*")
    elif typer.confirm("Would you like to add include paths?"):
        while True:
            included_paths.append(typer.prompt("Please enter a path to include in gitignore format (e.g /src)"))
            
            if not typer.confirm("Would you like to add another include path?"):
                break
    
    excluded_paths = list()
    if typer.confirm("Would you like to add exclude paths?"):
        while True:
            excluded_paths.append(typer.prompt("Please enter a path to exclude in gitignore format (e.g /node_modules)"))
            
            if not typer.confirm("Would you like to add another exclude path?"):
                break
    
    exclude_files = []
    for file in DEFAULT_IGNOREFILES:
        if Path(file).exists():
            exclude_files.append(file)
    
    with open(output, 'w') as file:
        file.write(TEMPLATE.format(
            version=__version__,
            include=dumps(included_paths),
            exclude=dumps(excluded_paths),
            exclude_files=dumps(exclude_files),
            name=project_name
        ).strip())


if __name__ == "__main__":
    app()
