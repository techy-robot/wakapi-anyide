from wakapi_anyide import main
from wakapi_anyide.models.config import WakatimeConfig
from wakapi_anyide.models.environment import Environment
from wakapi_anyide.models.project import Project
import typer
import asyncio

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
    raise NotImplementedError()

if __name__ == "__main__":
    app()