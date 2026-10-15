FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir pipenv

COPY Pipfile Pipfile.lock ./
RUN pipenv install --system --deploy

COPY task.py validator.py checksum.py ./
COPY schemas ./schemas

CMD ["python3", "task.py"]
