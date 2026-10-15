# Torreta

Cliente torrent simples para Windows (funciona também em Linux/macOS), em português, **sem anúncios, sem telemetria e sem conta**.

Feito depois de dissecar o uTorrent Web com o [REA](https://github.com/morluto/rea) para entender o que ele faz por dentro — e corrigir o que ele faz de errado.

## O que ele faz

- Adiciona por **link magnet**, **URL de `.torrent`**, **arquivo `.torrent`** (botão, arrastar-e-soltar ou Ctrl+V na janela).
- Pausar, retomar, verificar, abrir pasta, remover (com ou sem apagar os arquivos).
- **Escolher quais arquivos baixar** dentro do torrent, com progresso por arquivo.
- **Baixar em ordem** (sequencial) para assistir a um vídeo enquanto ele baixa.
- Limites de velocidade, porta de escuta e pasta de downloads configuráveis.
- **Trackers públicos adicionados automaticamente** a torrents não privados (mais fontes de peers; torrents privados nunca os recebem — em magnets, só depois dos metadados confirmarem que não é privado). Desligável em ⚙.
- Ajuda para **limitar o upload a 80% da sua subida**, o que evita que o download caia em conexões assimétricas.
- DHT, PEX, LSD, uTP, UPnP/NAT-PMP e criptografia de peers ligados por padrão (engine **libtorrent 2.x**, o mesmo do qBittorrent e do próprio uTorrent Web).

## Em que ele é melhor que o uTorrent Web

| Problema encontrado no uTorrent Web (evidência) | Torreta |
|---|---|
| Anúncios, detector de bloqueador de anúncios (`BlockAdBlock`), upsell de "PRO+VPN" por US$ 69,99 | Nenhum. Não existe código de monetização. |
| Google Analytics (`gtag`), `http_report`, checagem de atualização em servidores da BitTorrent Inc. | Só fala com trackers, DHT e peers dos seus torrents. |
| Disco cheio → 5.186 erros repetidos no log no mesmo milissegundo, download trava sem aviso | Checa o espaço livre **antes** de gravar (ao adicionar, quando chegam os metadados e quando você muda os arquivos). Se não couber, pausa e explica quanto falta. |
| `Can't open resume file` → perdia o estado da sessão | Resume data de cada torrent em arquivo próprio, gravado atomicamente a cada minuto e no fechamento. |
| Porta 6881 ocupada por outro programa → fica mudo | Escolhe automaticamente a próxima porta livre e avisa na interface. |
| Credencial fixa em `users.conf` + token CSRF | Token aleatório por execução; API só em `127.0.0.1`. |
| Arquivos de preenchimento `.pad` aparecendo | Ocultos. |

## Como rodar

Pré-requisito: Python 3.11+ (o da Microsoft Store serve).

```bat
torreta.cmd
```

Na primeira execução ele cria o `.venv` e instala as dependências (`libtorrent`, `fastapi`, `uvicorn`, `pywebview`). Depois abre a janela do Torreta.

Outras formas:

```bat
torreta.cmd "magnet:?xt=urn:btih:..."          :: já adiciona ao abrir
torreta.cmd arquivo.torrent
torreta.cmd --no-window                        :: abre no navegador em vez da janela
.venv\Scripts\python -m torreta --help
```

Onde ficam as coisas:

- Downloads: `%USERPROFILE%\Downloads\Torreta` (mude nas configurações ⚙).
- Configuração, resume data e avisos: `%APPDATA%\Torreta`.

## Estrutura

```
torreta/
  engine.py     invólucro do libtorrent: sessão, espaço em disco, resume data, prioridades
  server.py     API HTTP local (FastAPI) com token por execução
  config.py     config.json em %APPDATA%\Torreta
  __main__.py   sobe servidor + janela (pywebview) ou navegador
  static/       interface: HTML/CSS/JS puro, sem build
```

## Sobre velocidade

O que limita a velocidade é, nesta ordem: quantos seeds o torrent tem, a sua conexão (cabo > Wi-Fi 5 GHz > Wi-Fi 2.4 GHz) e se você aceita conexões de entrada (atrás de CGNAT, não aceita — só IP público, IPv6 ou VPN com *port forwarding* resolvem). Dos ajustes de software, só dois têm efeito comprovado e ambos estão no Torreta: trackers públicos extras e limite de upload em ~80% da subida. "Presets de alto desempenho", milhares de conexões e afins não movem esse teto e foram deixados de fora de propósito.

## Limitações / próximos passos

- Sem ícone na bandeja: fechar a janela encerra o cliente (gravando o estado antes).
- Sem RSS, agendador nem criação de torrents.
- Sem player embutido: use "baixar em ordem" e abra o arquivo no seu player.
- Instalador `.exe` (PyInstaller) e associação de `magnet:`/`.torrent` ao app ainda não feitos.

## Contribuindo

Pull requests são bem-vindos. Use [Conventional Commits](https://www.conventionalcommits.org/pt-br/) nas mensagens (`feat:`, `fix:`, `docs:`, `chore:`…). Não envie arquivos `.torrent` nem dados de sessão — o `.gitignore` já os bloqueia.

## Licença

[MIT](LICENSE). O engine [libtorrent](https://www.libtorrent.org/) é BSD; [pywebview](https://pywebview.flowrl.com/) é BSD; [FastAPI](https://fastapi.tiangolo.com/) é MIT.

## Uso responsável

Baixe apenas conteúdo que você tem direito de baixar.
