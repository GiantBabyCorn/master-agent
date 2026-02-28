from pydantic import BaseModel, ConfigDict, Field


class TelegramUser(BaseModel):
    id: int
    is_bot: bool | None = None
    first_name: str | None = None
    username: str | None = None


class TelegramChat(BaseModel):
    id: int
    type: str


class TelegramMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: int
    from_: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    text: str | None = None
    date: int


class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_: TelegramUser | None = Field(default=None, alias="from")
    message: TelegramMessage | None = None
    data: str | None = None


class TelegramUpdate(BaseModel):
    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None
