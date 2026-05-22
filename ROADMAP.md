# TC3D Gallery Roadmap

## Estado actual

La galeria ya funciona como visor privado de proyectos con login, filtros, enlaces temporales y gestion basica de usuarios.

## Hecho hasta ahora

- Login con usuarios y contrasenas hasheadas.
- Roles `admin`, `client` y `social`.
- Ocultacion de rutas reales de imagen mediante IDs internos.
- Miniaturas y entrega optimizada de imagenes desde servidor.
- Etiqueta `Nuevo` limitada a proyectos de los ultimos 7 dias.
- Coste oculto por defecto con boton para mostrarlo.
- Visibilidad especifica para `Redes Sociales` por proyecto.
- Enlaces temporales con caducidad, por defecto 10 dias.
- Clientes autorizados para crear enlaces temporales.
- Portada limitada a los 20 proyectos mas recientes para aligerar entrada.
- Agrupacion de variantes `OP1`, `OP2`, etc. dentro de versiones.
- Agrupacion `OP1`, `OP2` tambien en enlaces temporales nuevos.
- Mejora de robustez en primera carga para evitar pantallas negras por sesion caducada.
- Carga inicial optimizada: la portada recibe resumen y el detalle se carga al abrir proyecto.

## Siguiente fase recomendada

- Revisar despliegue del NAS para asegurar que usa la ultima version del codigo.
- Añadir copia de seguridad/exportacion de usuarios y ajustes desde panel admin.
- Crear paginacion o `Cargar mas` en portada para bibliotecas grandes.
- Añadir logs simples de errores para detectar fallos de carga en clientes.
- Preparar sincronizacion limpia entre local, NAS y GitHub.
- Separar mejor datos privados y codigo para publicar el repo sin riesgo.

## Prioridad alta

- Confirmar que el NAS esta sirviendo esta misma version.
- Dejar el repositorio GitHub listo y ordenado.
- Verificar enlaces temporales y carga de renders con usuarios reales.
