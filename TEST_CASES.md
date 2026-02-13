# Test Cases

## Objetivo

Validar que la tipificacion automatica respete reglas funcionales para cuidador.

## Checklist por lote

1. Subir ZIP masivo (estructura: ZIP -> carpeta -> PDFs).
2. Iniciar tipificacion.
3. Verificar estados por paquete (`done/error`).
4. Descargar `TIPIFICADO_LOTE.zip`.
5. Validar nombres de salida por factura (`<FACTURA>.zip` y PDFs por categoria).

## Reglas funcionales clave

- `OPF`: solo Orden Medica (Decisiones).
- `HEV`: Historia Clinica + Trabajo Social + Registro de Actividades de Cuidado.
- `CRC`: Registro de Atencion Domiciliaria (incluyendo continuaciones por tabla).
- `FEV`: Factura electronica de venta, detalle de cargos y nota credito cuando exista.
- `PDE`: Autorizacion de servicios.

## Casos especiales

- Prefijos de factura variables: `OCFE`, `CUFE`, `BUFE`, etc.
- El NIT y numero de factura deben salir del documento correcto de FEV.
- Si hay Nota Credito, debe quedar incluida en FEV.

## Criterios de aceptacion

- Ninguna pagina de Historia Clinica o Trabajo Social en OPF.
- CRC sin paginas ajenas a Registro de Atencion Domiciliaria.
- HEV ordenado por `FECHA DE CREACION` (antiguo -> reciente) cuando aplique.
- ZIP descargable por paquete y consolidado.

