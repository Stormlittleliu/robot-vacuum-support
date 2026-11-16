import os
from abc import ABC, abstractmethod
from typing import Optional

from langchain_community.chat_models.tongyi import BaseChatModel, ChatTongyi

from core.observability import latency, provider_transport
from core.utils.config_handler import rag_conf

from dotenv import load_dotenv
load_dotenv()


class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Optional[BaseChatModel]:
        pass


def _build_deepseek_model():
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，请在项目根目录 .env 中配置")
    timeout = float(rag_conf.get("request_timeout", 60))
    base_url = rag_conf.get("chat_base_url", "https://api.deepseek.com")
    with latency.span("model.load.chat", provider="deepseek", model=rag_conf["chat_model_name"]):
        model = ChatOpenAI(
            model=rag_conf["chat_model_name"],
            base_url=base_url,
            api_key=api_key,
            temperature=float(rag_conf.get("chat_temperature", 0)),
            # 构造时传入：事后赋值不会传导到已建好的 client
            request_timeout=timeout,
        )
        if provider_transport.enabled():
            # Opt-in only.  LangChain/OpenAI already built the real default sync
            # client (socket options, proxy mounts, trust_env, TLS).  Wrap that
            # exact instance; do not pass http_client and do not build a second
            # transport.  The async client is intentionally out of scope.
            try:
                provider_transport.instrument_client(
                    getattr(getattr(model, "root_client", None), "_client", None)
                )
            except Exception:
                # Diagnostics must never prevent the model from loading.
                pass
        return model


class ChatModelFactory(BaseModelFactory):
    def generator(self) -> Optional[BaseChatModel]:
        provider = str(rag_conf.get("chat_provider", "dashscope")).lower()

        if provider == "deepseek":
            return _build_deepseek_model()

        return ChatTongyi(model=rag_conf["chat_model_name"], temperature=0)


chat_model = ChatModelFactory().generator()
