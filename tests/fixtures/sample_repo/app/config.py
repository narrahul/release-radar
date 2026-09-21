"""Settings for the sample app."""

import os
from pydantic import BaseSettings


class Settings(BaseSettings):
    debug: bool = False
    home: str = os.environ.get("HOME", "")
