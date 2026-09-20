def classFactory(iface):
    from .deps import bootstrap_sys_path
    bootstrap_sys_path()
    from .plugin_main import DroneCoregPlugin
    return DroneCoregPlugin(iface)
