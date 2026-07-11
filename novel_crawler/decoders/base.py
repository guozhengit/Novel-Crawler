from abc import ABC, abstractmethod


class TextDecoder(ABC):
    @abstractmethod
    def decode(self, text: str) -> str:
        raise NotImplementedError


class MappingDecoder(TextDecoder):
    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping = mapping or {}

    def decode(self, text: str) -> str:
        if not self.mapping:
            return text
        return "".join(self.mapping.get(ch, ch) for ch in text)


class DecoderPipeline(TextDecoder):
    def __init__(self, decoders: list[TextDecoder]):
        self.decoders = decoders

    def decode(self, text: str) -> str:
        for decoder in self.decoders:
            text = decoder.decode(text)
        return text
