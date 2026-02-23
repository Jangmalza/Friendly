from typing import Any


def get_network_broker() -> Any:
    # Defer import to runtime to avoid import cycles during app startup.
    from app import main as app_main

    return app_main.network_broker
