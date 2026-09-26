import logging
import re


class RedactedFormatter(logging.Formatter):
    def __init__(self, config):
        super().__init__("%(asctime)s %(levelname)s pid=%(process)d %(name)s %(message)s")
        self.secrets = [
            config.get(k, "") for k in ("bot_token", "convex_key", "convex_deploy_key", "topgg_token")
        ]

    def format(self, record):
        text = super().format(record)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return re.sub(r"(/webhooks/\d+/)[\w.-]+", r"\1[REDACTED]", text)


def configure(config, name):
    handlers = [logging.StreamHandler()]
    for handler in handlers:
        handler.setFormatter(RedactedFormatter(config))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("anchorbot").setLevel(logging.DEBUG if config["detailed_logging"] else logging.INFO)

    logging.getLogger("hikari").setLevel(logging.INFO if config["detailed_logging"] else logging.WARNING)
