"""Estilos compartidos de la interfaz."""

# Selección de fila claramente visible también sin foco, respetando el tema
# (palette(highlight)/palette(highlighted-text) siguen el color del sistema).
TABLE_SELECTION_QSS = (
    "QTableView::item:selected {"
    " background-color: palette(highlight);"
    " color: palette(highlighted-text);"
    "}"
)
