# pinga-ana-adventure-demo

Demo **pygame-ce** empacotada com **pygbag** (async + `await asyncio.sleep(0)` no loop).

## Se o site abrir em branco ou só mostrar este README

No GitHub: **Settings → Pages → Source: GitHub Actions**. Enquanto a origem for uma **branch** com `README.md` na raiz, o GitHub usa **Jekyll** — não o `index.html` do pygbag.

## Site na org `pinga-ana` (`pinga-ana.com`)

- Repositório do apex: **`pinga-ana/pinga-ana.github.io`** (custom domain e certificado HTTPS nas **Settings → Pages** desse repo).
- **Variable** `USER_SITE_REPO` → `pinga-ana/pinga-ana.github.io`
- **Variable** opcional `USER_SITE_PATH` → omite para publicar em **`pinga-ana-adventure-demo/`** na raiz do repo do site (site estático; URL `https://pinga-ana.com/pinga-ana-adventure-demo/`). Para um site **Hugo**, define `USER_SITE_PATH` como `static/pinga-ana-adventure-demo` (o Hugo copia `static/` para `public/`).
- **Secret** `USER_PAGES_TOKEN` → PAT com permissão de **push** no repo `.github.io` da org.

Com `USER_SITE_REPO` definida, o workflow faz **clone + `git add -f`** na subpasta indicada (por omissão `pinga-ana-adventure-demo/` na raiz).

---

## Apex + pasta (ex.: site pessoal ou Hugo)

O GitHub **não** publica o Pages **deste** repositório de projeto no URL do apex quando o domínio já é servido pelo repositório **`<owner>.github.io`**. O redirect pode existir, mas o conteúdo do jogo nunca chega a essa pasta — daí o 404.

**Solução:** copiar o `build/web` para dentro do repo **`<owner>.github.io`**, numa subpasta servida pelo teu gerador estático (ex.: **`static/pinga-ana-adventure-demo/`** num site **Hugo**). O pygbag usa caminhos relativos ao `index.html`, por isso a subpasta no URL mantém-se.

### 1. Repositório `<owner>.github.io`

- **Custom domain** e DNS ficam nas **Settings → Pages** desse repo (não no do jogo).

### 2. Neste repo (`pinga-ana-adventure-demo`)

**Settings → Secrets and variables → Actions** (nível do **repositório** — não só *Environments → github-pages*: o workflow usa `vars.USER_SITE_REPO`.)

| Tipo | Nome | Valor |
|------|------|--------|
| **Variable** | `USER_SITE_REPO` | `pinga-ana/pinga-ana.github.io` (ou `owner/repo` do site no apex) |
| **Variable** (opcional) | `USER_SITE_PATH` | Ex.: `static/pinga-ana-adventure-demo` (Hugo); omite para `pinga-ana-adventure-demo` na raiz |
| **Variable** (opcional) | `USER_SITE_BRANCH` | Branch do site; por defeito `main` |
| **Secret** | `USER_PAGES_TOKEN` | PAT com **push** no repo `.github.io` (**só em Secrets**, nunca em Variables) |

**Importante:** o workflow lê **`secrets.USER_PAGES_TOKEN`**, não a variable homónima. Se o PAT estiver só em Variables, o clone do repo público `.github.io` pode parecer OK, mas o **push falha** com `Invalid username or token`. Copia o valor para **Secrets → USER_PAGES_TOKEN** e apaga a variable (PAT em Variables fica visível a quem gere o repo).

O passo **Validar USER_PAGES_TOKEN** falha cedo se o secret estiver vazio, expirado ou sem permissão **Contents → Read and write** em `pinga-ana/pinga-ana.github.io` (fine-grained PAT: autoriza SSO na org se pedido).

Com `USER_SITE_REPO` **definida**, o workflow **deixa de** usar “GitHub Pages deste repo” e faz **clone + `git add -f`** na pasta de destino. O `-f` evita que o **`.gitignore` do site** remova ficheiros do pygbag (`.apk`, `.wasm`, `.js`, etc.).

### Depois de alterares variables ou o PAT

**Guardar variables não dispara o workflow.** Tens de:

1. **Actions** → workflow **Deploy pygbag to GitHub Pages** → **Run workflow** → **Run workflow**, **ou**
2. Fazer um **push** qualquer à branch `main` (por exemplo um commit vazio: `git commit --allow-empty -m "chore: trigger pages" && git push`).

No log do job **build**, o passo **Resumo do modo de deploy** deve mostrar se `USER_SITE_REPO` foi lido (se continuar “vazio”, a variable está noutro sítio ou com nome errado). Falta de **USER_PAGES_TOKEN** falha ainda no job **build**, antes do push para o `.github.io`.

### 3. Ajustar Pages **deste** repo do jogo

Para não haver redirect estranho nem conflito:

- **Settings → Pages → Custom domain**: remove / deixa vazio neste repositório.
- Opcional: desliga Pages neste repo se já não precisares do URL `github.io/pinga-ana-adventure-demo/`.

### 4. Branch do site

O workflow publica na branch **`main`** por omissão. Se o teu `.github.io` usar outra (ex.: `master`), cria a variable **`USER_SITE_BRANCH`** com esse nome (e confirma que a expressão no workflow é suportada; se falhar, edita `.github/workflows/pygbag-pages.yml` e fixa `publish_branch`).

Depois de um push com sucesso, o jogo deve abrir no URL da subpasta no teu domínio apex (ex.: **`https://pinga-ana.com/pinga-ana-adventure-demo/`** com a org `pinga-ana`).

### 5. A URL mostra o README (Jekyll) ou o layout do blog (Hugo) em vez do jogo

- **Jekyll (GitHub Pages “branch”):** se existir **`…/README.md`** na pasta publicada e **não** existir **`index.html`**, o Jekyll pode transformar o README em página.

- **Hugo (site em `afonsoaugusto.github.io` com Actions):** o deploy publica só o conteúdo de **`public/`** após `hugo`. Ficheiros na **raiz** do repo (ex.: `pinga-ana-adventure-demo/` fora de `static/`) **não** entram no site — o caminho `/pinga-ana-adventure-demo/` fica sem o bundle e vês página do tema (404, lista vazia, etc.). O bundle tem de estar em **`static/pinga-ana-adventure-demo/`** para o Hugo copiar para **`public/pinga-ana-adventure-demo/`**.

O workflow **remove `README.md`** da pasta de deploy em cada job e exige **`index.html`**. Confirma no GitHub que a pasta de destino (ex.: `pinga-ana-adventure-demo/` ou `static/pinga-ana-adventure-demo/` no Hugo) contém `index.html` (e `.apk`/`.tar.gz`, etc.). Se só aparecer `.nojekyll`, o deploy do Actions ainda não correu com sucesso ou falhou antes do push.

Opcional: **`.nojekyll` na raiz** do repo `.github.io` desactiva o Jekyll para **todo** o site quando a origem do Pages for uma branch (útil em sites estáticos sem Hugo Actions).

## Sem `USER_SITE_REPO` (só GitHub Pages deste repo)

Com a variable **vazia / não definida**, o fluxo antigo mantém-se: artefacto → **Deploy pygbag to GitHub Pages** → URL `https://<user>.github.io/pinga-ana-adventure-demo/`.

## Build local

```bash
pip install -r requirements.txt
python -m pygbag --build --template ci/default.tmpl --icon ci/favicon.png .
```

Saída em `build/web/`.

**Nota:** em `pygbag.ini`, não uses entradas em `ignoreDirs` com **espaços** no caminho (ex.: `/assets/Aseprite file`) — o pygbag aborta antes de gerar `index.html`. O CI usa template e ícone em `ci/` para não depender só do CDN.
