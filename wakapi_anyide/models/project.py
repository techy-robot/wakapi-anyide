from typing import List, Literal
from pydantic import BaseModel
from pydantic_settings import BaseSettings, TomlConfigSettingsSource
from pydantic_settings.main import SettingsConfigDict

class ProjectMeta(BaseModel):
    version: Literal[1]

class ProjectFiles(BaseModel):
    include: List[str]
    exclude: List[str]
    exclude_files: List[str]
    
class ProjectDescription(BaseModel):
    name: str

class Project(BaseSettings):
    model_config = SettingsConfigDict(toml_file="wak.toml")
    
    meta: ProjectMeta
    files: ProjectFiles
    project: ProjectDescription
    
    @classmethod
    def settings_customise_sources(cls, settings_cls, *_args, **_kwargs):
        return (TomlConfigSettingsSource(settings_cls), )