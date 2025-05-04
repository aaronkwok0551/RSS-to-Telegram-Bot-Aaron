# src/main.py

if __name__ == "__main__":
    # 嘗試使用新版本的啟動入口 (entrypoint)
    try:
        from src import entrypoint
    except ImportError:
        # 如果沒有 entrypoint 模組，退而求其次直接執行舊版路徑 src.web
        import importlib
        importlib.import_module("src.web")
    else:
        # 如果找得到 entrypoint，且內有 main 函數，則呼叫它啟動 bot
        if hasattr(entrypoint, "main"):
            entrypoint.main()
