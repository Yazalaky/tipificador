# Tipificador Cloud

Aplicacion para tipificar soportes PDF de facturacion domiciliaria y generar ZIPs por paquete/categoria.

## Estructura

- `backend/`: API FastAPI (OCR, clasificacion, procesamiento de ZIP).
- `frontend/`: UI React + Vite.
- `tools/`: scripts de deploy y mantenimiento.
- `scripts/`: utilidades de bootstrap para entorno nuevo.
- `CONTEXT.md`: reglas funcionales del negocio.

## Documentacion operativa

- `RUN_LOCAL.md`: levantar proyecto en local.
- `DEPLOY.md`: despliegue a Cloud Run/Firebase.
- `OPERATIONS.md`: mantenimiento, cleanup, costos y logs.
- `TEST_CASES.md`: checklist de validacion funcional.

## Flujo rapido

1. Leer `CONTEXT.md`.
2. Ejecutar `scripts/bootstrap_dev.sh`.
3. Levantar backend y frontend siguiendo `RUN_LOCAL.md`.

