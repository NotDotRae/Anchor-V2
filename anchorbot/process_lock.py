import os


class ProcessLock:
    def __init__(self, path):
        self.file = open(path, "a+b")

    def __enter__(self):
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            self.file.truncate(0)
            self.file.write(b"0")
            self.file.flush()
        except OSError:
            self.file.close()
            raise RuntimeError("Another AnchorBot supervisor is using this database") from None
        return self

    def __exit__(self, *args):
        self.file.close()
