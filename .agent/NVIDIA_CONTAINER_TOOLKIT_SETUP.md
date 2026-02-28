# NVIDIA Container Toolkit Setup

GPU passthrough for Docker containers — required to run LLMs (or any CUDA workload) inside containers.

## Prerequisites

- NVIDIA GPU with drivers installed on the host
- Docker installed
- Linux host (or WSL2 on Windows)

## 1. Install NVIDIA Container Toolkit

```bash
# Add the NVIDIA GPG key
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

# Add the repository
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Install
sudo apt update && sudo apt install -y nvidia-container-toolkit
```

## 2. Configure Docker Runtime

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

## 3. Verify Installation

```bash
# Run a quick test
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

You should see your GPU(s) listed in the output.

## 4. Usage

### Basic — all GPUs

```bash
docker run --gpus all your-image:latest
```

### Specific GPU(s)

```bash
# Single GPU by index
docker run --gpus '"device=0"' your-image:latest

# Multiple GPUs
docker run --gpus '"device=0,1"' your-image:latest
```

### With security isolation

```bash
docker run -it \
  --gpus all \
  --network none \
  --read-only \
  --tmpfs /tmp \
  -v /path/to/workspace:/workspace \
  -v /path/to/models:/models:ro \
  --memory 32g \
  --cpus 8 \
  your-image:latest
```

| Flag | Purpose |
|---|---|
| `--gpus all` | Full GPU access |
| `--network none` | No network access (prevents data exfiltration) |
| `--read-only` | Container filesystem is read-only |
| `-v ...:/workspace` | Mount only the project directory |
| `-v ...:/models:ro` | Models available as read-only |
| `--memory` / `--cpus` | Resource limits |

## 5. Isolated Network (GPU + LLM server only)

If the container needs access to a local LLM API but not the internet:

```bash
# Create an internal-only network
docker network create --internal agent-net

# Start the LLM server
docker run -d \
  --gpus all \
  --network agent-net \
  --name llm-server \
  vllm/vllm-openai:latest \
  --model your-model

# Start the agent (can reach llm-server but not the internet)
docker run -it \
  --network agent-net \
  --name cursor-agent \
  your-image:latest
```

## 6. Troubleshooting

```bash
# Check NVIDIA driver on host
nvidia-smi

# Check toolkit installation
nvidia-ctk --version

# Check Docker runtime config
docker info | grep -i nvidia

# Test GPU inside container
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 \
  python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### Common issues

- **"could not select device driver"** — toolkit not installed or Docker not restarted after configuration
- **"no NVIDIA GPU device is present"** — host NVIDIA drivers not installed or GPU not detected
- **Permission errors** — add your user to the `docker` group: `sudo usermod -aG docker $USER`
