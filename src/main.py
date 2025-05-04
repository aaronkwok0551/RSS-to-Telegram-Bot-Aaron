if __name__ == "__main__":
    try:
        from src import entrypoint
    except ImportError:
        import importlib
        importlib.import_module("src.web")
    else:
        if hasattr(entrypoint, "main"):
            entrypoint.main()
