from py_clob_client_v2.clob_types import BuilderApiKey as BuilderApiKeyCreds
from py_clob_client_v2.clob_types import BuilderConfig as _BuilderConfig


class BuilderConfig(_BuilderConfig):
    def __init__(self, localBuilderCreds=None, builderCode=None):
        code = str(builderCode or "0x" + "0" * 64)
        super().__init__(builder_code=code)
        self.localBuilderCreds = localBuilderCreds
        self.builderCode = code


__all__ = ["BuilderApiKeyCreds", "BuilderConfig"]
