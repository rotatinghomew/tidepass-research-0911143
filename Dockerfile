ARG PYTHON_IMAGE=python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . /app
RUN useradd --create-home analyst && mkdir -p /app/state && chown -R analyst:analyst /app
USER analyst
EXPOSE 8080
CMD ["python", "-m", "tidepass", "--host", "0.0.0.0"]
