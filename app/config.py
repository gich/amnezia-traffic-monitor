try:
    import tomllib  # stdlib in Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # backport for Python 3.10
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AwgConfig:
    container: str
    interface: str
    config_path: str
    binary: str = "awg"


@dataclass
class CollectorConfig:
    poll_interval_seconds: int
    sample_retention_days: int


@dataclass
class DbConfig:
    path: str


@dataclass
class WebConfig:
    host: str
    port: int


@dataclass
class Config:
    awg: AwgConfig
    collector: CollectorConfig
    db: DbConfig
    web: WebConfig


def load_config(path: str) -> Config:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return Config(
        awg=AwgConfig(**data["awg"]),
        collector=CollectorConfig(**data["collector"]),
        db=DbConfig(**data["db"]),
        web=WebConfig(**data["web"]),
    )
