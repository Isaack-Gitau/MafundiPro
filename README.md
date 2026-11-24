# MafundiPro



MafundiPro is a marketplace backend designed to connect skilled workers with individuals and businesses seeking their services.



The platform is being developed with an emphasis on secure account management, structured worker and employer profiles, job discovery, and reliable marketplace operations.



**Development status:** Active development. APIs, database schemas, and features may change. This repository currently documents the backend implementation.



## Technology Stack



- Python 3.13+

- FastAPI

- Pydantic v2 and pydantic-settings

- PostgreSQL 17

- SQLAlchemy 2.x

- Alembic

- Argon2 password hashing

- Pytest and HTTPX

- Ruff



## Implemented Backend Areas



- Account registration, authentication, and session management

- Worker and employer profiles

- Profession, category, and skill taxonomy

- Location management

- Job advertisement creation and public discovery

- Job advertisement moderation and expiry

- Job posting credits and credit accounting

- Payment initiation and callback processing infrastructure

- Payment reconciliation and refund records

- Administrative job advertisement pricing configuration and change history



Feature availability and API contracts may change during development.



## Prerequisites



- Python 3.13 or newer

- PostgreSQL 17, or Docker with Docker Compose

- Git



## Local Development Setup



### 1. Clone the repository



```powershell

git clone https://github.com/Isaack-Gitau/MafundiPro.git

cd MafundiPro

```



### 2. Configure environment variables



Copy the example environment file:



```powershell

Copy-Item .env.example .env

```



Edit `.env` and configure:



- `POSTGRES_DB`

- `POSTGRES_USER`

- `POSTGRES_PASSWORD`

- `POSTGRES_HOST`

- `POSTGRES_PORT`

- `AUTH_SECRET_KEY`



Generate a unique authentication secret locally. Never commit `.env` or share its contents.



Ensure that the database connection settings match the PostgreSQL port exposed by Docker Compose.



### 3. Start PostgreSQL



From the repository root:



```powershell

docker compose up -d postgres

docker compose ps

```



The Compose configuration binds PostgreSQL to the local loopback interface and stores database data in a named Docker volume.



### 4. Create a Python virtual environment



From the repository root:



```powershell

py -3.13 -m venv .venv

..venv\Scripts\Activate.ps1

```



### 5. Install backend dependencies



```powershell

Set-Location backend

python -m pip install --upgrade pip

pip install -e ".[dev]"

```



### 6. Apply database migrations



Confirm that the configured database is the intended development database before applying migrations.



From the `backend` directory:



```powershell

alembic upgrade head

```



**Important:** Migrations modify the database schema. Review migration changes and prepare an appropriate backup before applying them to an existing or production database.



### 7. Start the API



From the `backend` directory:



```powershell

uvicorn app.main:app --reload

```



Default local endpoints:



- API root: `http://127.0.0.1:8000/`

- Interactive documentation: `http://127.0.0.1:8000/docs`

- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`

- Liveness: `http://127.0.0.1:8000/health/live`

- Readiness: `http://127.0.0.1:8000/health/ready`



Configure `FRONTEND_ORIGINS` to include the origins of frontend applications that need to communicate with the API.



## Running Tests



Tests use a dedicated PostgreSQL database named `mafundipro_test`. The test configuration includes safeguards intended to prevent tests from running against the normal development database.



Create the test database if it does not already exist. For PostgreSQL running through the supplied Compose configuration:



```powershell

docker exec mafundipro-postgres psql -U mafundipro_app -d postgres -c "CREATE DATABASE mafundipro_test OWNER mafundipro_app;"

```



From the `backend` directory, apply migrations to the test database:



```powershell

$env:POSTGRES_DB = "mafundipro_test"

alembic upgrade head

Remove-Item Env:POSTGRES_DB

```



Run tests:



```powershell

pytest -q

```



Run linting:



```powershell

ruff check app alembic tests

```



Check formatting:



```powershell

ruff format --check app alembic tests

```



Never configure the test suite to use a personal, shared, staging, or production database.



## Payment Processing



The backend contains payment initiation, callback handling, payment event records, refund accounting, and reconciliation functionality.



The default provider is `FakePaymentProvider`. It is intended for development and testing. It does not communicate with Safaricom or initiate real payments.



The presence of payment endpoints does not mean that production payment integration is ready.



## Security



Security is an important design consideration for MafundiPro.



- Passwords are hashed using Argon2.

- Authentication uses server-managed sessions.

- Sensitive configuration belongs in environment variables.

- Database credentials and authentication secrets must never be committed.

- Database migrations should be reviewed before deployment.

- Payment callbacks and payment state changes require appropriate verification.



If you discover a security vulnerability, avoid publishing exploit details or sensitive information in a public issue. Contact the repository owner privately.



## Configuration



The root `.env.example` file documents the environment variables used by the backend.



The application loads configuration from the repository-root `.env` file. Alembic uses the same application settings for its database connection.



Never replace example placeholders with real credentials in a committed file.



## Contributing



Contributions, suggestions, and bug reports are welcome. Discuss substantial architectural or security-related changes before implementing them.



When submitting changes:



1. Keep changes focused and reviewable.

2. Add or update tests for behavior changes.

3. Run the test suite and Ruff checks.

4. Never include secrets, local databases, credentials, or generated development artifacts.

5. Document changes affecting API contracts, database schemas, or security behavior.



## License



No open-source license has been specified for this repository yet. Until a license is added, the source code remains subject to applicable copyright protections. Public visibility alone does not grant permission to reuse or redistribute the code.






