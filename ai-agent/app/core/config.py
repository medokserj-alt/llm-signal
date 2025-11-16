from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "AI Trading Agent"
    # сюда потом добавим остальные настройки (LLM, БД, API-ключи и т.д.)

    class Config:
        env_file = ".env"


settings = Settings()
