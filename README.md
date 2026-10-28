# Vhost Manager

Gestor local para administrar virtual hosts, certificados y entradas de
`/etc/hosts` de Apache instalado con Homebrew en macOS.

La aplicación ofrece una interfaz gráfica en el navegador y conserva una CLI
para automatización. No necesita frameworks web ni dependencias de Python de
terceros.

## Funcionalidades

- Lista los `VirtualHost` configurados en Apache.
- Crea, modifica y elimina vhosts administrados por la aplicación.
- Configura `ServerName`, aliases, `DocumentRoot`, puerto y SSL.
- Genera certificados autofirmados con OpenSSL cuando se activa SSL y no existe
  todavía el par de certificado y clave.
- Añade, actualiza o elimina bloques administrados en `/etc/hosts`.
- Permite gestionar `/etc/hosts` también para vhosts escritos manualmente, sin
  modificar su configuración de Apache.
- Crea copias de seguridad antes de modificar los archivos de configuración.
- Incluye comandos para validar la configuración y recargar Apache.

Los bloques creados por la aplicación se identifican mediante comentarios:

```apache
# BEGIN vhost-manager: ejemplo.test
...
# END vhost-manager: ejemplo.test
```

Los vhosts sin estos marcadores aparecen como **Manual**. Su configuración se
muestra en modo de solo lectura para evitar sobrescribir bloques ajenos a la
aplicación.

## Requisitos

- macOS.
- Python 3.9 o posterior con la biblioteca estándar.
- Apache instalado mediante Homebrew.
- OpenSSL disponible en `PATH` para generar certificados.
- El archivo `httpd-vhosts.conf` incluido desde la configuración principal de
  Apache.

Una instalación típica de Apache es:

```bash
brew install httpd
```

Comprueba que `httpd.conf` incluye el archivo de vhosts y que los módulos
necesarios para SSL están habilitados si vas a utilizar HTTPS.

## Inicio rápido

Desde el directorio del proyecto:

```bash
python3 vhost-manager.py
```

También se puede ejecutar desde el botón **Run** de PyCharm.

El programa:

1. Detecta automáticamente el prefijo de Homebrew.
2. Inicia un servidor HTTP en una dirección aleatoria de `127.0.0.1`.
3. Abre la interfaz en el navegador predeterminado.
4. Lee los vhosts desde `httpd-vhosts.conf`.

La interfaz solo escucha en la máquina local y utiliza un token distinto en cada
ejecución para autorizar las operaciones. El proceso debe permanecer activo
mientras se utiliza la página. Para cerrarlo, pulsa `Ctrl+C` o detén la ejecución
desde el IDE.

También puedes abrir explícitamente la interfaz con:

```bash
python3 vhost-manager.py gui
```

La interfaz experimental basada en Tk continúa disponible con:

```bash
python3 vhost-manager.py desktop
```

## Uso de la interfaz gráfica

### Crear un vhost

1. Pulsa **Nuevo**.
2. Introduce `ServerName` y `DocumentRoot`.
3. Añade aliases separados por comas si son necesarios.
4. Selecciona el puerto y activa SSL si procede.
5. Mantén marcada **Gestionar en /etc/hosts** para asociar el dominio y sus
   aliases a `127.0.0.1`.
6. Pulsa **Guardar**.

Si SSL está activado y no existe un certificado con el nombre indicado, la
aplicación genera automáticamente un certificado autofirmado y su clave privada.

### Modificar un vhost

Selecciona un vhost marcado como **Gestionado**, cambia sus datos y pulsa
**Guardar**. `ServerName` no se puede renombrar directamente: crea un vhost nuevo
y elimina el anterior.

### Vhosts manuales

Al seleccionar un vhost marcado como **Manual**, sus directivas permanecen en
solo lectura. Sí puedes activar **Gestionar en /etc/hosts** y pulsar
**Guardar hosts** para crear una entrada administrada sin alterar el bloque de
Apache.

## Permisos y copias de seguridad

El archivo de vhosts de Homebrew suele pertenecer al usuario que instaló Apache,
por lo que puede modificarse directamente. `/etc/hosts` pertenece a `root`.

Cuando una operación necesita escribir en `/etc/hosts`, macOS muestra el diálogo
del sistema para solicitar contraseña de administrador o Touch ID. La aplicación
solo eleva la instalación del archivo preparado; el servidor y el resto del
programa siguen ejecutándose con los permisos del usuario.

Las copias de seguridad se guardan en estas ubicaciones:

- `httpd-vhosts.conf.bak-<fecha>` junto al archivo de vhosts.
- `~/.config/vhost-manager/backups/hosts.bak-<fecha>` para `/etc/hosts`.

Si se cancela la autorización al crear un vhost, la modificación del archivo de
vhosts se revierte.

## Configuración de rutas

Por defecto se utilizan estas rutas:

```text
$(brew --prefix)/etc/httpd/extra/httpd-vhosts.conf
$(brew --prefix)/etc/httpd/certs
$(brew --prefix)/bin/apachectl
/etc/hosts
```

Muestra la configuración efectiva con:

```bash
python3 vhost-manager.py config show
```

Para guardar rutas diferentes:

```bash
python3 vhost-manager.py config set \
  --vhosts-file /ruta/httpd-vhosts.conf \
  --certs-dir /ruta/certs \
  --apachectl /ruta/apachectl \
  --hosts-file /ruta/hosts
```

La configuración se guarda por defecto en:

```text
~/.config/vhost-manager/config.json
```

También se puede seleccionar otro archivo para una ejecución concreta:

```bash
python3 vhost-manager.py --config /ruta/config.json gui
```

## Uso por terminal

### Vhosts

```bash
# Listar vhosts
python3 vhost-manager.py vhost list

# Mostrar un vhost
python3 vhost-manager.py vhost show ejemplo.test

# Crear un vhost HTTP
python3 vhost-manager.py vhost add ejemplo.test \
  --document-root /ruta/proyecto/public \
  --alias www.ejemplo.test \
  --map-host

# Crear un vhost HTTPS
python3 vhost-manager.py vhost add seguro.test \
  --document-root /ruta/proyecto/public \
  --ssl \
  --map-host

# Creación interactiva
python3 vhost-manager.py vhost new

# Eliminar un vhost administrado
python3 vhost-manager.py vhost remove ejemplo.test

# Validar y recargar Apache
python3 vhost-manager.py vhost test
python3 vhost-manager.py vhost reload
```

### `/etc/hosts`

```bash
# Listar entradas
python3 vhost-manager.py host list

# Añadir un bloque administrado
python3 vhost-manager.py host add ejemplo.test --alias www.ejemplo.test

# Eliminar un bloque administrado
python3 vhost-manager.py host remove ejemplo.test
```

### Certificados

```bash
# Listar certificados y claves
python3 vhost-manager.py cert list

# Generar un certificado autofirmado
python3 vhost-manager.py cert generate ejemplo.test \
  --san www.ejemplo.test \
  --days 825

# Importar un certificado existente
python3 vhost-manager.py cert install ejemplo.test \
  --cert /ruta/certificado.crt \
  --key /ruta/clave.key

# Eliminar el certificado y la clave
python3 vhost-manager.py cert remove ejemplo.test
```

Consulta todas las opciones con:

```bash
python3 vhost-manager.py --help
python3 vhost-manager.py vhost add --help
python3 vhost-manager.py cert generate --help
```

## Pruebas

Ejecuta la suite con:

```bash
python3 -m unittest -v
```

Si utilizas el entorno virtual incluido en el proyecto:

```bash
.venv/bin/python -m unittest -v
```
