from pydantic import BaseModel, Field


class User(BaseModel):
    name: str = Field(default="anon")
