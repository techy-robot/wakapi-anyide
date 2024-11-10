from pathlib import Path
from wakapi_anyide import __version__
from wakapi_anyide.cli import main
from wakapi_anyide.models.config import WakatimeConfig
from wakapi_anyide.models.environment import Environment
from wakapi_anyide.models.project import Project
from json import dumps
import typer
import asyncio

DEFAULT_IGNOREFILES = [".gitignore"]
TEMPLATE = """
# https://github.com/iamawatermelo/wakapi-anyide v{version}

[meta]
version = 1

[files]
include = {include}  # files to include in tracking
exclude = {exclude}  # files to exclude in tracking
exclude_files = {exclude_files}  # files whose contents will be used to exclude other files from tracking

[project]
name = "{name}"  # your project name
"""

app = typer.Typer()

@app.command()
def test():
    asyncio.run(main(Environment(
        is_test_only=True,
        config=WakatimeConfig(),  # type: ignore
        project=Project()  # type: ignore
    )))
    
@app.command()
def track():
    asyncio.run(main(Environment(
        is_test_only=False,
        config=WakatimeConfig(),  # type: ignore
        project=Project()  # type: ignore
    )))
    
@app.command()
def setup():
    output = Path("wak.toml")
    if output.exists():
        raise Exception("a wak.toml already exists in this directory")
        
    project_name = typer.prompt("What's your project name?", default=Path("./").absolute().name)
    
    included_paths = list()
    if typer.confirm("Would you like to add include paths?"):
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