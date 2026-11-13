# LiveHub 0.3.3

## Segredos protegidos

- Tokens e chaves (Twitch, StreamElements e LivePix) agora ficam guardados criptografados com a protecao do Windows: so o seu usuario do Windows, neste PC, consegue le-los.
- A migracao e automatica: na primeira abertura, o arquivo antigo e regravado criptografado, sem voce fazer nada.
- Se copiar a pasta de dados para outro PC (ou outro usuario do Windows), sera preciso entrar de novo na Twitch e colar as chaves outra vez. O arquivo que nao puder ser lido e guardado como `secrets.json.ilegivel-<data>`, nunca apagado.

## Diagnostico

- Ao salvar as configuracoes, o `livehub.log` registra quais secoes foram salvas e quais credenciais mudaram (sem nunca gravar os valores), e tambem o motivo quando uma configuracao e recusada.
