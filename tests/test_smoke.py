def test_skeleton_imports() -> None:
    import app.api  # noqa: F401
    import app.engine  # noqa: F401
    import app.events  # noqa: F401
    import app.models  # noqa: F401
    import app.rules  # noqa: F401
    import app.service  # noqa: F401
    import app.store  # noqa: F401

    from app.service import RiskMonitoringService

    svc = RiskMonitoringService()
    assert svc.store is not None
