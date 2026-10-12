# Proxy inverso dinamico con monitoreo

Scaffold de infraestructura con Caddy, Vector y PostgreSQL + TimescaleDB.
`backend/` y `frontend/` quedan vacios para su desarrollo posterior.

## Servicios

- **Caddy** escucha en los puertos 80 y 443, obtiene y renueva certificados
  HTTPS automaticamente para `servicio1.icc.com`, `servicio2.icc.com` y
  `servicio3.icc.com`, registra accesos JSON y expone su Admin API unicamente
  dentro de la red de Docker.
- **Plano de control** ofrece una API local autenticada para agregar, actualizar
  y eliminar servicios. Persiste el estado deseado en SQLite y lo sincroniza con
  Caddy sin reiniciar el proxy.
- Los tres servicios existentes enrutan respectivamente a `10.8.66.235:8081`,
  `10.8.66.235:8082` y `10.8.66.235:8083`.
- **Vector** lee los logs de Caddy, conserva timestamp, dominio, IP cliente,
  metodo y estado HTTP, y los envia en lotes a TimescaleDB y al webhook.
- **TimescaleDB** crea la extension y la hypertable `proxy_requests` al iniciar
  por primera vez.

## Inicio

```sh
cp .env.example .env
```

Edita `.env`, establece una contraseña segura y genera un token API largo:

```sh
openssl rand -hex 32
```

Pon el resultado como `DYNAMIC_PROXY_API_TOKEN` en `.env` y luego ejecuta:

```sh
docker compose up -d --build
docker compose ps
```

La inicializacion SQL solo se ejecuta cuando el volumen de PostgreSQL esta
vacio. Si cambias el esquema mas adelante, aplica una migracion; no borres el
volumen para forzarla si necesitas conservar datos.
Los tres servicios existentes se cargan desde `control-plane/initial-services.json`
solo en la primera ejecucion del plano de control; a partir de ahi, SQLite es
la fuente persistente del catalogo.

## Administrar servicios en caliente

La API se publica solo en `127.0.0.1` del host y requiere el token configurado
en `.env`. Ejemplo para agregar un cuarto servicio:

```sh
curl -X POST http://127.0.0.1:8080/services \
  -H "Authorization: Bearer $DYNAMIC_PROXY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "domain": "servicio4.icc.com",
    "upstream_ip": "10.8.66.235",
    "upstream_port": 8084
  }'
```

La respuesta confirma `"applied": true` cuando Caddy acepto el cambio. Los
dominios deben ser subdominios de `icc.com`; el destino debe ser una IPv4
privada enrutable y el puerto un entero entre 1 y 65535. Si Caddy no esta
disponible, el servicio queda guardado y el plano de control vuelve a intentar
sin reiniciar los contenedores.

Listar los servicios:

```sh
curl http://127.0.0.1:8080/services \
  -H "Authorization: Bearer $DYNAMIC_PROXY_API_TOKEN"
```

Eliminar uno:

```sh
curl -X DELETE http://127.0.0.1:8080/services/servicio4.icc.com \
  -H "Authorization: Bearer $DYNAMIC_PROXY_API_TOKEN"
```

El estado deseado se guarda en el volumen `control_plane_data`. Caddy tambien
persiste la ultima configuracion aplicada en `caddy_config` y se inicia con esa
configuracion; al arrancar, el plano de control reconcilia nuevamente sus
servicios con Caddy. No elimines esos volumenes si quieres conservar rutas y
certificados.

## Agregar destinos dinamicamente

La Admin API de Caddy (`caddy:2019`) permanece sin publicar en el host. Usa la
API autenticada del plano de control; no conectes clientes externos directamente
a la Admin API.

## DNS, HTTPS y certificados

Caddy solicita y renueva certificados publicos bajo demanda solo para dominios
registrados en el plano de control. Cada nuevo subdominio necesita un registro
DNS `A` (y `AAAA` solo si el servidor tambien tiene IPv6 funcional) hacia la IP
publica del proxy antes de que se solicite el certificado. El servidor debe
aceptar conexiones entrantes por 80/tcp y 443/tcp desde Internet; 443/udp queda
publicado para HTTP/3. Los servicios de origen deben ser accesibles desde el
contenedor de Caddy.

Los certificados y sus claves privadas quedan en el volumen persistente
`caddy_data`; el endpoint de autorizacion de certificados solo aprueba dominios
registrados. Si `icc.com` o los subdominios no estan bajo tu control o no
resuelven hacia este servidor, la emision automatica no funcionara.

## Logs y webhook

Vector manda los registros a TimescaleDB con un lote maximo de 500 eventos o
un segundo, y al webhook con un lote maximo de 100 eventos o un segundo. El
webhook apunta al servicio Laravel futuro (`backend:8000/api/metrics`). Como
ese servicio aun no existe, Vector reintentara la entrega del webhook hasta
que se implemente y este disponible. La escritura en TimescaleDB funciona de
forma independiente.

Vector recibe las credenciales de PostgreSQL por variables de entorno y habilita
explicitamente su interpolacion en la configuracion. Usa valores controlados
por el operador y una contraseña URL-safe (letras, numeros, guiones o guiones
bajos) para el endpoint PostgreSQL.

Consulta el estado y los logs con:

```sh
docker compose logs -f caddy control-plane vector postgres
docker compose exec postgres psql -U proxy -d proxy_monitor \
  -c 'SELECT * FROM proxy_requests ORDER BY timestamp DESC LIMIT 10;'
```

El comando usa los valores predeterminados; si cambiaste el usuario o la base
en `.env`, sustituyelos. Los datos se conservan en volumenes de Docker. Protege
`.env` y respalda el volumen `postgres_data` segun tus necesidades de retencion.
