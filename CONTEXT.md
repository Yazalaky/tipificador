# Tipificador Cloud Reminder (Context)

## Objetivo
Aplicación web para tipificar páginas de uno o varios PDFs pertenecientes a una factura.
El usuario clasifica páginas en: CRC, FEV, HEV, OPF, PDE. Luego se genera un ZIP con PDFs por categoría.

## Naming final
{PREFIJO}_{NIT_BASE}_{OCFE}.pdf
- NIT_BASE se extrae de FEV (NIT: 900204617-5 -> 900204617)
- OCFE se extrae de FEV (OCFE5871)

## Backend
- FastAPI (backend/app/main.py)
- Endpoints:
  - POST /jobs (subida de múltiples PDFs)
  - GET /jobs/{id}/pages/{page}/thumb.png
  - GET /jobs/{id}/pages/{page}/view.png
  - POST /jobs/{id}/process (genera ZIP)
- Procesamiento PDF con PyMuPDF

## Frontend
- React + Vite (frontend/src/App.tsx)
- Permite seleccionar páginas y asignar categorías
- Llama API definida por VITE_API_BASE
