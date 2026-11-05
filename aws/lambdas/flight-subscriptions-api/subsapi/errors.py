"""API errors: a stable machine code, an HTTP status and optional details (contract §3.6)."""


class ApiError(Exception):
    def __init__(self, status, code, details=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.details = details or {}
