"""Estilos compartidos de la interfaz."""

# Selección de fila claramente visible también sin foco, respetando el tema
# (palette(highlight)/palette(highlighted-text) siguen el color del sistema).
TABLE_SELECTION_QSS = (
    "QTableView {"
    " selection-background-color: palette(highlight);"
    " selection-color: palette(highlighted-text);"
    "}"
)
