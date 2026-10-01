# Comparativo de Protocolos de Roteamento (RIP, OSPF) e Algoritmo Próprio

Este repositório contém um laboratório prático de redes desenvolvido com **Containerlab** e **Docker**. O objetivo é comparar o comportamento dos protocolos tradicionais (**RIP** e **OSPF**) com o de um **algoritmo próprio orientado à latência real (RTT)**.

---

## Objetivo do projeto

Os protocolos tradicionais utilizam métricas estáticas — contagem de saltos no RIP ou largura de banda nominal no OSPF — e ignoram a latência real de um enlace.

Neste cenário, foi inserida uma latência artificial de **60 ms** no atalho direto, representado pela diagonal entre `R1` e `R3`:

- **RIP e OSPF:** escolhem a diagonal por ser um caminho de um salto ou por apresentar a mesma largura de banda nominal, direcionando todo o tráfego por um caminho lento, de aproximadamente **60 ms**.
- **Algoritmo próprio:** identifica a latência real via ICMP e desvia o tráfego pelo anel periférico, alcançando uma latência de aproximadamente **20 ms**.

---

## Topologia e latência

A rede é formada por um anel de cinco roteadores (`R1` a `R5`), com uma ligação diagonal entre `R1` e `R3`.

- **Diagonal `R1–R3`:** latência injetada via `tc netem`, de aproximadamente **60 ms RTT**.
- **Anel periférico (`R1–R2–R3`):** latência acumulada de aproximadamente **20 ms RTT**.

---

## Pré-requisitos e instalação

### Dependências

- Docker
- Containerlab

Para instalar o Containerlab, execute:

```bash
curl -sL https://get.containerlab.dev | sudo bash
```

### Build das imagens

Execute os comandos abaixo uma única vez:

```bash
sudo docker build -t lab-fso-router:latest -f images/Dockerfile.router images/
sudo docker build -t lab-fso-host:latest -f images/Dockerfile.host images/
```

### Subir o laboratório

```bash
sudo containerlab deploy -t topo.clab.yml
```

---

## Alternar entre protocolos

O script `switch-protocol.sh` altera o protocolo em execução na rede.

### Ativar OSPF v2

```bash
./switch-protocol.sh ospf
```

### Ativar RIP v2

```bash
./switch-protocol.sh rip
```

### Ativar o algoritmo próprio

```bash
./switch-protocol.sh proprio
```

---

## Testes e coleta de resultados

### 1. Teste de latência — H1 para H3

Execute o comando abaixo para medir a latência entre os hosts:

```bash
sudo docker exec -it clab-lab-fso-h1 ping -c 10 192.168.3.10
```

Resultados esperados:

- **OSPF/RIP:** RTT médio de aproximadamente **60 ms**.
- **Algoritmo próprio:** RTT médio de aproximadamente **20 ms**.

### 2. Teste de falha e monitoramento contínuo

Em um terminal, execute o monitor de falhas do algoritmo próprio:

```bash
python3 algoritmo_proprio.py --watch
```

Em outro terminal, simule a queda da interface `eth1` em `R1`:

```bash
sudo docker exec -it clab-lab-fso-r1 ip link set eth1 down
```

**Comportamento esperado:** o monitoramento detecta a queda em aproximadamente **1 segundo** e recalcula as rotas na tabela do kernel (FIB) em aproximadamente **0,9 segundo**.

---

## Destruir o ambiente

Para encerrar os contêineres e limpar as interfaces virtuais, execute:

```bash
sudo containerlab destroy -t topo.clab.yml
```

---

## Estrutura do repositório

- `topo.clab.yml`: definição da topologia no Containerlab.
- `algoritmo_proprio.py`: script do algoritmo guiado por RTT, com injeção direta de rotas no kernel por meio de `ip route`.
- `switch-protocol.sh`: script de alternância entre os cenários e aplicação de atraso via `tc netem`.
- `configs/`: arquivos de configuração do FRRouting para OSPF e RIP.

---

## Vídeo de demonstração

Link para o vídeo de demonstração do projeto:

> [Acessar o vídeo de demonstração](https://drive.google.com/drive/folders/1OESwAVcIwHzwa3XYOZm5FzV6oXIdHxD1?usp=sharing)
