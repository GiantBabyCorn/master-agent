from pydantic import BaseModel, ConfigDict, Field


class TelegramUser(BaseModel):
    id: int
    is_bot: bool | None = None
    first_name: str | None = None
    username: str | None = None


class TelegramChat(BaseModel):
    id: int
    type: str


class TelegramDocument(BaseModel):
    file_id: str
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TelegramPhotoSize(BaseModel):
    file_id: str
    width: int
    height: int
    file_size: int | None = None


class TelegramMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: int
    from_: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    text: str | None = None
    date: int
    document: TelegramDocument | None = None
    photo: list[TelegramPhotoSize] | None = None
    caption: str | None = None


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
