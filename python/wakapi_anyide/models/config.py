from os import environ
from pathlib import Path

from pydantic import BaseModel
from pydantic.deprecated.class_validators import root_validator
from pydantic_settings import BaseSettings
from pydantic_settings.main import SettingsConfigDict

from wakapi_anyide.helpers.ini import IniConfigSettingsSource


class Settings(BaseModel):
    api_key: str | None = None
    api_key_vault_cmd: str | None = None
    api_url: str = "https://api.wakatime.com/api/v1"
    hostname: str | None = None
    heartbeat_rate_limit_seconds: int = 120
    
    @root_validator(pre=True)
    def validate_api_key(cls, values):
        if key := environ.get("WAKATIME_API_KEY"):
            values["api_key"] = key
        
        if values.get('api_key') == None and values.get('api_key_vault_cmd') == None:
            raise ValueError('One of api_key or api_key_vault_cmd must be set.')
        
        return values


class WakatimeConfig(BaseSettings):
    model_config = SettingsConfigDict(
        ini_file=Path(environ.get("WAKATIME_HOME", environ.get("HOME", "~/"))) / ".wakatime.cfg",
        extra='ignore'
    )  # type: ignore
    
    settings: Settings
    
    @classmethod
    def settings_customise_sources(cls, settings_cls, *_args, **_kwargs):
        return (IniConfigSettingsSource(settings_cls), )
