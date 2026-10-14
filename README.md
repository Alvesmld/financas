# Finanças Domésticas — versão revisada

## O que foi melhorado
- Cadastro automático após compra aprovada na Cakto: o webhook registra o e-mail do comprador e a pessoa cria a conta com o mesmo e-mail, sem precisar de chave.
- O webhook tenta reconhecer formatos comuns de payload e evita duplicar eventos quando existe ID do pedido.
- Campo de chave virou opcional; compra por e-mail ou ativação por chave continuam suportadas.
- Removidas credenciais de demonstração pré-preenchidas e a senha mestra padrão insegura.
- Nova área de recomendações financeiras: alerta quando as saídas consomem grande parte da renda, destaca a categoria de maior gasto e aponta saldo negativo.
- IA recebe resumo de entradas, saídas e gastos por categoria para gerar respostas mais úteis.
- Camada de provedores de IA: tenta Gemini, APIs compatíveis com OpenAI e Ollama conforme a ordem configurada, seguindo para o próximo provedor se o anterior falhar.
- Endpoint `/api/health/ia` mostra quais provedores e modelos estão configurados.
- Validação básica para não registrar transações com valor zero/negativo ou tipo inválido.
- Mensagens de erro da IA não revelam detalhes internos do servidor.
- Escape de conteúdo em alguns pontos da interface para reduzir risco de injeção de HTML.
- O banco não foi incluído no ZIP para não distribuir dados de usuários. O app cria/migra as tabelas no primeiro arranque.

## Deploy no Render
1. Substitua os arquivos do serviço pelos arquivos deste ZIP e faça deploy.
2. Configure as variáveis de ambiente:
   - `GEMINI_API_KEY`: sua chave da API Gemini, se quiser usar o chat IA e scanner.
   - `AI_PROVIDERS`: ordem de tentativa, separada por vírgulas. Padrão: `gemini,openai,ollama`. Valores aceitos: `gemini`, `openai`, `openai_compatible`, `openrouter`, `lmstudio`, `ollama`.
   - `GEMINI_MODEL`: opcional; padrão `gemini-2.5-flash`.
   - `GEMINI_FALLBACK_MODEL`: opcional; padrão `gemini-2.0-flash`.
   - `OPENAI_API_KEY`: chave para OpenAI ou para um provedor compatível que exija autenticação.
   - `OPENAI_BASE_URL`: opcional; padrão `https://api.openai.com/v1`. Para outro provedor, informe a URL base compatível com a API Chat Completions, terminando normalmente em `/v1`.
   - `OPENAI_MODEL`: nome exato do modelo no provedor; padrão `gpt-4o-mini`.
   - `OLLAMA_BASE_URL`: endereço HTTP do Ollama acessível pelo backend; padrão `http://127.0.0.1:11434`.
   - `OLLAMA_MODEL`: modelo já baixado no Ollama; padrão `llama3.2:3b`.

### Como configurar múltiplos modelos

Exemplo usando Gemini como primeira opção, um provedor compatível com OpenAI como segunda e Ollama como terceira:

```env
AI_PROVIDERS=gemini,openai,ollama
GEMINI_API_KEY=sua_chave_gemini
GEMINI_MODEL=gemini-2.5-flash
GEMINI_FALLBACK_MODEL=gemini-2.0-flash
OPENAI_API_KEY=sua_chave_do_provedor
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
OLLAMA_BASE_URL=http://IP-DO-SERVIDOR-OLLAMA:11434
OLLAMA_MODEL=llama3.2:3b
```

O backend tenta os provedores na ordem definida e passa ao seguinte quando falta configuração ou há falha. Não precisa configurar todos: deixe em `AI_PROVIDERS` apenas os que realmente pretende usar.

**Modelos locais:** Ollama e LM Studio podem executar modelos localmente. Porém, se o backend estiver hospedado no Render, `127.0.0.1` aponta para o próprio container do Render, não para seu computador. Para usar um modelo no seu PC, o backend precisa rodar nesse PC ou o serviço hospedado precisa alcançar um endpoint remoto seguro. Não exponha o Ollama diretamente à internet sem autenticação, firewall ou túnel privado. O scanner de imagens/PDF ainda usa Gemini; os outros provedores são usados pelo chat de texto.
4. Para verificar a configuração da IA, abra `https://SEU-SERVICO.onrender.com/api/health/ia`. Isso confirma se a variável foi carregada e se o SDK está instalado; não garante que a chave tenha quota/permissão.
5. O chat e o scanner agora registram o motivo técnico nos logs do Render. Se continuar falhando, abra **Logs** no Render no momento do teste.
   - `CAKTO_WEBHOOK_SECRET`: segredo forte aleatório. Use o mesmo segredo no webhook, por header `X-Webhook-Secret` ou parâmetro `?secret=...`, se a configuração da Cakto permitir.
   - `CORS_ORIGINS`: origens exatas do frontend, separadas por vírgula, apenas se frontend e backend estiverem em domínios diferentes. Ex.: `https://seu-site.vercel.app`.
   - `DATABASE_PATH`: caminho do arquivo SQLite, opcional.
   - `SENHA_FAMILIAR_MESTRA`: opcional; deixe ausente se não usa acesso administrativo compartilhado.
3. No Render, configure o webhook da Cakto para `https://SEU-SERVICO.onrender.com/api/webhook/cakto` (ou adicione `?secret=SEU_SEGREDO` se for usar segredo por URL).
6. Faça uma compra de teste e confira os logs do Render. O webhook precisa receber o e-mail do comprador e um status/evento equivalente a `approved`, `paid`, `order.paid`, `payment.approved`, `purchase_approved` ou `completed`.
7. Depois da compra, o comprador abre o site, escolhe **Ativar Licença Cakto**, deixa a chave vazia e usa exatamente o e-mail que informou na compra. Cria uma senha com pelo menos 8 caracteres e depois faz login.

## Muito importante sobre o webhook da Cakto
Os nomes exatos dos campos variam conforme o evento configurado na Cakto. Esta versão reconhece campos comuns, mas o primeiro teste real é obrigatório. Se o payload enviado pela sua Cakto tiver outros nomes/caminhos, o endpoint responderá que não encontrou o e-mail. Compare o payload/documentação do evento e ajuste os campos no início da função `webhook_cakto`.

## Persistência de dados no Render
SQLite pode perder dados em novos deploys/restarts se o serviço não tiver disco persistente configurado. Para produção, configure um Persistent Disk no Render e aponte `DATABASE_PATH` para o caminho montado no disco. Não apague nem substitua o banco de produção sem backup.

## Limitações e pontos de atenção
- Esta revisão não confirma uma compra real na Cakto: isso só pode ser validado com um evento real de teste.
- Se o frontend for hospedado em domínio separado, defina `CORS_ORIGINS` com o domínio exato do frontend.
- Antes de vender em escala, ainda é recomendável reforçar a autenticação de todas as rotas de dados com token de sessão. Algumas rotas legadas recebem `usuario_id` pela URL/corpo; não trate isso como segurança suficiente contra acesso indevido.
- O sistema não integra automaticamente com bancos (Open Finance); a IA analisa apenas lançamentos que já estão no app ou documentos enviados.
- Recomendações são baseadas nos lançamentos registrados. Se os dados estiverem incompletos, as conclusões também estarão incompletas.
