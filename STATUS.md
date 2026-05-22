# Resumen del proyecto

## Objetivo

Galeria privada online para mostrar renders y proyectos a clientes, con control por usuario y opcion de compartir enlaces temporales.

## Cambios principales hechos

- Seguridad:
  contrasenas en hash y ocultacion de rutas reales de imagen.

- Gestion:
  nuevo rol `Redes Sociales` y control por proyecto.

- Visual:
  sello `Nuevo` solo para la ultima semana y coste oculto por defecto.

- Comparticion:
  enlaces temporales mas utiles, con 10 dias por defecto y agrupacion de opciones `OP1`, `OP2`.

- Rendimiento:
  menos carga al entrar y mas estabilidad en la primera apertura.

## Riesgos a revisar

- El NAS puede seguir usando una version antigua si no se reinicia o no se actualiza.
- `users.json`, enlaces temporales y otros datos privados no conviene publicarlos tal cual en GitHub.
- `data.js` es un archivo grande y generado; conviene decidir si debe vivir en el repo o regenerarse.
