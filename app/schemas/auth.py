from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class UserResponse(BaseModel):
    id: str
    username: str
    department: str
    avatar_url: str | None = None
    can_manage_accounts: bool = False


class LoginResponse(BaseModel):
    user: UserResponse

