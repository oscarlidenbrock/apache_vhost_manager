# Vhost Manager

Gestor gráfico local de virtual hosts para Apache instalado con Homebrew. Muestra los
vhosts existentes y permite crear, editar y eliminar de forma segura los bloques
creados por la aplicación. Antes de cada cambio se genera una copia de respaldo.

## Abrir la aplicación

```bash
python3 vhost-manager.py
```

La aplicación abre automáticamente el navegador y solo escucha en `127.0.0.1`;
los datos no salen del equipo. Por defecto detecta las rutas de Homebrew. También
se pueden guardar rutas alternativas con el comando `config set`:

- archivo `httpd-vhosts.conf`;
- directorio de certificados;
- ejecutable `apachectl`;
- archivo `hosts`.

Cuando una operación necesita modificar `/etc/hosts`, macOS solicita autorización
de administrador. La aplicación solo eleva la instalación de ese archivo y guarda
antes una copia en `~/.config/vhost-manager/backups`.

Los vhosts escritos manualmente también aparecen en el listado, pero se mantienen
en modo de solo lectura para evitar sobrescribir configuración ajena a la
aplicación.

La interfaz de escritorio Tk anterior sigue disponible como alternativa con
`python3 vhost-manager.py desktop`.

## Uso por terminal

La interfaz de comandos anterior continúa disponible. Por ejemplo:

```bash
python3 vhost-manager.py vhost list
python3 vhost-manager.py vhost add ejemplo.test --document-root /ruta/public
python3 vhost-manager.py vhost test
```

Para mostrar todos los comandos:

```bash
python3 vhost-manager.py --help
```

## Pruebas

```bash
python3 -m unittest -v
```
