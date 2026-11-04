"""行情 Loader 共用的入参/数据校验异常（四个 loader 唯一定义点）。"""


class DataValidationError(ValueError):
    """Loader validation error for unparseable dates/inputs."""
