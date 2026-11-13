"""End-to-end: roda o app de verdade e dirige as paginas no Microsoft Edge (Playwright). Sem rede real."""
import asyncio
import re

import pytest
from aiohttp import web

pytest.importorskip("playwright")
from playwright.async_api import async_playwright, expect  # noqa: E402

from livehub import __version__, runtime, settings  # noqa: E402
from livehub.resgates import audio, const  # noqa: E402
from livehub.runtime import Runtime  # noqa: E402
from conftest import porta_livre  # noqa: E402
from test_dispositivo import Fake, OK  # noqa: E402

pytestmark = pytest.mark.e2e
VIEWS = ["inicio", "twitch", "kick", "youtube", "tiktok", "streamelements", "livepix", "sons", "aparencia", "alertas", "links", "avancado"]


def _plat(nome):
    class P:
        def __init__(self, cfg, hub, session):
            self.hub = hub

        async def run(self):
            await self.hub.set_status(nome, "conectado (teste)")
            await asyncio.Event().wait()
    return P


@pytest.fixture
async def app(monkeypatch):
    """Runtime real, Twitch falsa local, plataformas e audio simulados."""
    f = Fake()
    fr = web.AppRunner(f.app)
    await fr.setup()
    site = web.TCPSite(fr, "127.0.0.1", 0)
    await site.start()
    fbase = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(const, "TWITCH_ID", fbase)
    monkeypatch.setattr(const, "TWITCH_DEVICE", fbase + "/oauth2/device")
    monkeypatch.setattr(const, "TWITCH_API", fbase + "/helix")
    monkeypatch.setattr(const, "EVENTSUB_URL", "ws://127.0.0.1:1/eventsub")
    monkeypatch.setattr(runtime, "PLATAFORMAS",
                        {k: (_plat(k), a) for k, (_, a) in runtime.PLATAFORMAS.items()})
    monkeypatch.setattr(audio, "listar_saidas", lambda: ["Saída Fake"])
    tocados = []
    monkeypatch.setattr(audio, "tocar_dados", lambda *a: tocados.append(a))
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    rt = Runtime()
    await rt.start()

    async def dormir(t):
        await asyncio.sleep(0.05)
    rt.svc.auth._sleep = dormir
    rt.fake, rt.tocados, rt.url = f, tocados, f"http://127.0.0.1:{cfg['web_port']}"
    yield rt
    await rt.stop()
    await fr.cleanup()


@pytest.fixture
async def nav():
    async with async_playwright() as p:
        try:
            b = await p.chromium.launch(channel="msedge")
        except Exception as e:  # Edge indisponivel
            pytest.skip(f"Microsoft Edge indisponivel: {type(e).__name__}")
        yield b
        await b.close()


@pytest.fixture
async def abrir(nav, app):
    """abrir(caminho, largura) -> page com coleta de erros de console/pagina."""
    ctxs = []

    async def _abrir(caminho="/config", largura=1280):
        ctx = await nav.new_context(viewport={"width": largura, "height": 900})
        ctxs.append(ctx)

        async def so_local(route):
            if route.request.url.startswith(("http://127.0.0.1", "data:", "about:")):
                await route.continue_()
            else:
                await route.abort()
        await ctx.route("**/*", so_local)
        page = await ctx.new_page()
        page.erros = []
        page.on("console", lambda m: m.type == "error" and page.erros.append("console: " + m.text))
        page.on("pageerror", lambda e: page.erros.append("pageerror: " + str(e)))
        await page.goto(app.url + caminho)
        return page
    yield _abrir
    for c in ctxs:
        await c.close()


async def test_config_carrega_sem_erros_e_navega(abrir, app):
    page = await abrir()
    await expect(page.locator("#v-inicio")).to_be_visible()
    await expect(page.locator("#versao")).to_have_text(f"versão {__version__}")
    for v in VIEWS:
        await page.click(f'.it[data-v="{v}"]')
        await expect(page.locator(f"#v-{v}")).to_be_visible()
        await expect(page.locator(".view.on")).to_have_count(1)
        assert page.url.endswith("#" + v)
    await page.wait_for_timeout(300)
    assert page.erros == []


async def test_deep_link_hash(abrir):
    page = await abrir("/config#kick")
    await expect(page.locator("#v-kick")).to_be_visible()
    await expect(page.locator("#v-inicio")).to_be_hidden()
    p2 = await abrir("/config#streamelements")
    await expect(p2.locator("#v-streamelements")).to_be_visible()


async def test_inicio_status_e_checklist(abrir):
    page = await abrir()
    await expect(page.locator("#pcards .card")).to_have_count(6)
    await expect(page.locator("#falta")).to_contain_text("Colocar os canais")
    await expect(page.locator("#falta")).to_contain_text("entrar com a Twitch")
    await expect(page.locator("#qu_dock")).to_contain_text("/dock")
    await page.click("#falta button:has-text('Dispensar')")
    await expect(page.locator("#falta")).not_to_contain_text("Adicionar o chat")


async def test_editar_mostra_barra_e_salva(abrir, app):
    page = await abrir("/config#youtube")
    await expect(page.locator("#barra")).to_be_hidden()
    await page.fill("#yt_h", "@meucanal")
    await expect(page.locator("#barra")).to_be_visible()
    await page.click("#btn_desc_alt")  # descartar
    await expect(page.locator("#barra")).to_be_hidden()
    await expect(page.locator("#yt_h")).to_have_value("")
    await page.fill("#yt_h", "https://www.youtube.com/@meucanal")
    await expect(page.locator("#barra")).to_be_visible()
    await page.click("#btn_salvar")
    await expect(page.locator("#barra")).to_be_hidden()
    assert settings.load_config()["youtube"]["handle"] == "meucanal"
    j = await page.evaluate("fetch('/api/config').then(r=>r.json())")
    assert j["config"]["youtube"]["handle"] == "meucanal"
    assert page.erros == []


async def test_erro_de_validacao_mantem_barra(abrir):
    page = await abrir("/config#avancado")
    await page.fill("#a_port", "80")
    await page.click("#btn_salvar")
    await expect(page.locator("#barra")).to_be_visible()
    await expect(page.locator("#msg")).to_contain_text("porta")


async def test_twitch_opcao2_e_login_por_codigo(abrir, app):
    page = await abrir("/config#twitch")
    await expect(page.locator("#op2")).to_contain_text("Sem login você perde")
    await expect(page.locator("#op2")).to_contain_text("Continua funcionando")
    await expect(page.locator("#op1")).to_contain_text("RECOMENDADO")
    await page.click("#btn_entrar")
    await expect(page.locator("#tw_cod")).to_have_text("ABCDEFGH")
    await expect(page.locator("#tw_codigo")).to_be_visible()
    app.fake.token_respostas = [OK]
    await expect(page.locator("#tw_ok")).to_be_visible(timeout=15000)
    await expect(page.locator("#tw_ok")).to_contain_text("Conectado como amigo")
    await expect(page.locator("#tw_canal")).to_have_value("amigo")
    await expect(page.locator("#barra")).to_be_hidden()
    await page.click("#btn_desc")
    await expect(page.locator("#tw_ok")).to_be_hidden(timeout=10000)
    assert page.erros == []


async def test_twitch_codigo_negado(abrir, app):
    app.fake.token_respostas = [(400, {"message": "access_denied"})]
    page = await abrir("/config#twitch")
    await page.click("#btn_entrar")
    await expect(page.locator("#tw_erro")).to_contain_text("recusou", timeout=10000)
    await expect(page.locator("#btn_entrar")).to_have_text("Tentar de novo")


async def test_sons_testar_e_pasta(abrir, app):
    page = await abrir("/config#sons")
    await expect(page.locator("#au_dev option")).to_have_text(["Padrão do sistema", "Saída Fake"])
    await page.select_option("#au_dev", "Saída Fake")
    await expect(page.locator("#barra")).to_be_visible()
    await page.click("text=Testar som")
    await expect(page.locator("#au_msg")).to_contain_text("bipe")
    assert app.tocados and app.tocados[0][3] == "Saída Fake"
    await page.click("#btn_salvar")
    await expect(page.locator("#barra")).to_be_hidden()
    assert settings.load_config()["audio"]["output_device"] == "Saída Fake"
    await page.click("#lnk_resg")
    await expect(page).to_have_url(app.url + "/recompensas")


async def test_aparencia_previa_e_salvar(abrir, app):
    page = await abrir("/config#aparencia")
    await page.wait_for_selector("#ap_cor_fundo")
    await page.fill("#ap_cor_fundo", "#123456")
    await page.fill("#ap_opacidade_fundo", "100")

    async def bg():
        for f in page.frames:
            if "/dock?demo=1" in f.url:
                return await f.evaluate(
                    "getComputedStyle(document.body).backgroundColor + '|' + "
                    "getComputedStyle(document.documentElement).backgroundColor + '|' + "
                    "[...document.querySelectorAll('*')].map(e=>getComputedStyle(e).backgroundColor).join('|')")
        return ""
    for _ in range(50):
        if "rgb(18, 52, 86)" in await bg():
            break
        await page.wait_for_timeout(100)
    assert "rgb(18, 52, 86)" in await bg()
    await expect(page.locator("#barra")).to_be_hidden()  # aparencia tem botoes proprios
    await page.click("text=Salvar aparência")
    await expect(page.locator("#ap_msg")).to_contain_text("Salvo")
    assert settings.load_config()["aparencia"]["dock"]["cor_fundo"] == "#123456"
    await page.click("#tab_neutro")
    await expect(page.locator("#apmold")).to_have_class("xadrez")
    assert page.erros == []


async def test_recompensas_sem_erros_e_voltar(abrir, app):
    page = await abrir("/recompensas")
    await expect(page.locator("h1")).to_contain_text("Resgates")
    await page.wait_for_timeout(500)
    await page.click("a.back")
    await expect(page).to_have_url(app.url + "/config")
    await expect(page.locator("#v-inicio")).to_be_visible()
    assert page.erros == []
    p2 = await abrir("/recompensas")
    await p2.click("#semapp a")
    await expect(p2.locator("#v-twitch")).to_be_visible()


async def test_viewport_estreito_navegavel(abrir):
    page = await abrir("/config", largura=700)
    for v in ("kick", "links", "aparencia", "inicio"):
        await page.click(f'.it[data-v="{v}"]')
        await expect(page.locator(f"#v-{v}")).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.erros == []


async def test_overlay_de_alertas_mostra_e_confirma_fim(abrir, app):
    app.cfg["alertas"]["categorias"]["follow"]["duracao"] = 2
    page = await abrir("/alertas")
    for _ in range(100):
        if app.alertas.estado()["clientes"] == 1:
            break
        await asyncio.sleep(0.05)
    assert app.alertas.estado()["clientes"] == 1
    r = await page.evaluate("""() => fetch('/api/alertas/testar', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({categoria: 'follow'})}).then(r => r.status)""")
    assert r == 200
    await expect(page.locator("#al")).to_contain_text("Teste")
    await expect(page.locator("#al")).to_contain_text("seguiu")
    assert app.alertas.estado()["atual"] is not None
    await expect(page.locator("#al")).not_to_be_visible(timeout=6000)  # animou para fora e mandou fim
    for _ in range(40):
        if app.alertas.estado()["atual"] is None:
            break
        await asyncio.sleep(0.05)
    assert app.alertas.estado()["atual"] is None
    assert page.erros == []


async def test_config_alertas_renderiza_e_salva(abrir, app):
    page = await abrir("/config#alertas")
    await expect(page.locator("#v-alertas")).to_be_visible()
    await expect(page.locator("#u_alertas")).to_have_text(app.url + "/alertas")
    await expect(page.locator("#al_cats .card")).to_have_count(6)
    campo = page.locator("#al_follow_texto")
    await expect(campo).to_have_value("{user} seguiu!")
    await campo.fill("{user} é novo por aqui!")
    await page.locator("#al_e_animacao").select_option("bounce")
    await expect(page.locator("#barra")).to_be_visible()
    await page.click("#btn_salvar")
    await expect(page.locator("#toast")).to_contain_text("Salvo")
    c = settings.load_config()["alertas"]
    assert c["categorias"]["follow"]["texto"] == "{user} é novo por aqui!" and c["estilo"]["animacao"] == "bounce"
    await page.reload()
    await expect(page.locator("#al_follow_texto")).to_have_value("{user} é novo por aqui!")
    assert page.erros == []


async def test_feed_botao_repetir_em_todas_as_linhas(abrir, app):
    await app.hub.interacao("twitch", "follow", "Ana", id="e1")
    await app.hub.interacao("twitch", "resgate", "Bia", "Hidratar", id="e2")
    await app.hub.interacao("twitch", "outro", "Cid", id="e3")
    page = await abrir("/eventos")
    await expect(page.locator(".m")).to_have_count(3)
    await expect(page.locator(".rp")).to_have_count(3)
    await expect(page.locator(".rp:disabled")).to_have_count(1)
    await expect(page.locator(".rp:disabled")).to_have_attribute("title", "Nada para repetir")
    await expect(page.locator(".rp:not(:disabled)").first).to_have_attribute("title", "Repetir")
    app.alertas.pausar(True)
    await page.locator(".m", has_text="Ana").locator(".rp").click(force=True)
    for _ in range(40):
        if app.alertas.fila:
            break
        await asyncio.sleep(0.05)
    assert len(app.alertas.fila) == 1
    assert page.erros == []


async def test_feed_barra_de_controles_pausa_e_pular(abrir, app):
    page = await abrir("/eventos", 360)
    await expect(page.locator("#ctl")).to_be_visible()
    await expect(page.locator("#pausa")).to_have_text("⏸ Pausar alertas")
    await expect(page.locator("#pausa")).to_have_attribute("aria-pressed", "false")
    await expect(page.locator("#pular")).to_be_disabled()  # sem alerta em exibicao
    await page.click("#pausa")
    await expect(page.locator("#pausa")).to_have_text("⏵ ALERTAS PAUSADOS — Retomar")
    await expect(page.locator("#pausa")).to_have_attribute("aria-pressed", "true")
    await expect(page.locator("body")).to_have_class("p")
    assert app.alertas.pausado is True
    outra = await abrir("/config#alertas")  # segunda pagina reflete o estado
    await expect(outra.locator("#al_pausar")).to_have_text("Retomar alertas")
    await outra.click("#al_pausar")  # e desfaz pela config; o feed acompanha via WS
    await expect(page.locator("#pausa")).to_have_text("⏸ Pausar alertas")
    assert app.alertas.pausado is False
    assert page.erros == [] and outra.erros == []


async def test_feed_pular_com_contagem_regressiva(abrir, app):
    app.alertas.atual = {"id": "a1", "texto": "x"}
    app.alertas._avisar()
    page = await abrir("/eventos", 360)
    await expect(page.locator("#pular")).to_be_enabled()
    await page.click("#pular")
    await expect(page.locator("#pular")).to_be_disabled()
    await expect(page.locator("#pular")).to_contain_text("Aguarde")
    await page.wait_for_timeout(3500)
    await expect(page.locator("#pular")).to_have_text("⏭ Pular alerta")
    await expect(page.locator("#pular")).to_be_enabled()
    assert page.erros == []


async def test_config_pular_tem_guarda_de_3s(abrir, app):
    page = await abrir("/config#alertas")
    await page.click("#al_pular")
    await expect(page.locator("#al_pular")).to_be_disabled()
    await expect(page.locator("#al_pular")).to_contain_text("Aguarde")
    assert page.erros == []


async def test_livepix_salva_e_mostra_placeholders(abrir):
    page = await abrir("/config")
    await page.click('button[data-v="livepix"]')
    await page.fill("#lp_id", "lp-id-e2e")
    await page.fill("#lp_sec", "lp-sec-e2e")
    await page.click("#btn_salvar")
    await expect(page.locator("#barra")).to_be_hidden()
    lp = settings.load_secrets()["livepix"]
    assert lp["client_id"] == "lp-id-e2e" and lp["client_secret"] == "lp-sec-e2e"
    await page.reload()
    await page.click('button[data-v="livepix"]')
    await expect(page.locator("#lp_id")).to_have_attribute("placeholder", "•••• salvo")
    await expect(page.locator("#lp_sec")).to_have_attribute("placeholder", "•••• salvo")
    assert page.erros == []


async def test_livepix_remover_credenciais(abrir):
    sec = settings.load_secrets()
    sec["livepix"].update(client_id="a", client_secret="b")
    settings.save_secrets(sec)
    page = await abrir("/config")
    await page.click('button[data-v="livepix"]')
    await page.check("#lp_rem")
    await page.click("#btn_salvar")
    await expect(page.locator("#barra")).to_be_hidden()
    lp = settings.load_secrets()["livepix"]
    assert lp["client_id"] == "" and lp["client_secret"] == ""
    assert page.erros == []


async def test_nav_todas_as_views_sem_erros(abrir):
    page = await abrir("/config")
    for v in VIEWS:
        await page.goto(page.url.split("#")[0] + "#" + v)
        await expect(page.locator(f"#v-{v}")).to_be_visible()
    await page.wait_for_timeout(300)
    assert page.erros == []


async def test_nav_grupo_plataformas_recolhe_e_persiste(abrir):
    page = await abrir("/config")
    tog = page.locator("#plat_tog")
    await expect(tog).to_have_attribute("aria-expanded", "true")
    await tog.click()
    await expect(tog).to_have_attribute("aria-expanded", "false")
    await expect(page.locator("#plat_sum")).to_be_visible()
    await expect(page.locator("#plat_sum")).to_have_text(re.compile(r"^[0-6]/6$"))
    await expect(page.locator('.it[data-v="twitch"]')).to_be_hidden()
    await page.reload()
    await expect(page.locator("#plat_tog")).to_have_attribute("aria-expanded", "false")
    await page.goto(page.url.split("#")[0] + "#twitch")
    await expect(page.locator("#plat_tog")).to_have_attribute("aria-expanded", "true")
    await expect(page.locator('.it[data-v="twitch"]')).to_be_visible()
    await page.locator("#plat_tog").click()
    await page.locator("#plat_tog").click()
    await expect(page.locator("#plat_tog")).to_have_attribute("aria-expanded", "true")
    assert page.erros == []


async def test_nav_submenu_alertas_e_scroll_tts(abrir):
    page = await abrir("/config")
    sub = page.locator('.smenu[data-for="alertas"]')
    await expect(sub).to_be_hidden()
    await page.click('.it[data-v="alertas"]')
    await expect(sub).to_be_visible()
    await page.click('.sit[data-view="alertas"][data-sec="tts"]')
    assert page.url.endswith("#alertas/tts")
    await expect(page.locator('.sit[data-sec="tts"]')).to_have_class("sit on")
    await page.wait_for_timeout(1200)
    box = await page.locator("#sec_al_tts").bounding_box()
    assert 0 <= box["y"] < 900
    await page.click('.it[data-v="links"]')
    await expect(sub).to_be_hidden()
    p2 = await abrir("/config#alertas/tts")
    await expect(p2.locator("#v-alertas")).to_be_visible()
    await p2.wait_for_timeout(1200)
    assert 0 <= (await p2.locator("#sec_al_tts").bounding_box())["y"] < 900
    assert page.erros == [] and p2.erros == []


async def test_barra_sujo_indicador_e_ctrl_s(abrir):
    page = await abrir("/config")
    await expect(page.locator("#barra")).to_be_hidden()
    await page.click('.it[data-v="youtube"]')
    await page.fill("#yt_h", "@canalteste")
    await expect(page.locator("#barra.dirty")).to_be_visible()
    await expect(page.locator("#btn_salvar")).to_have_text("Salvar alterações")
    await expect(page.locator('.it[data-v="youtube"] .ns')).to_be_visible()
    await expect(page.locator('.it[data-v="kick"] .ns')).to_be_hidden()
    await page.keyboard.press("Control+s")
    await expect(page.locator("#barra")).to_be_hidden()
    await expect(page.locator('.it[data-v="youtube"] .ns')).to_be_hidden()
    assert settings.load_config()["youtube"]["handle"] == "canalteste"
    assert page.erros == []


async def test_livepix_teste_ok_mas_sujo_pede_salvar(abrir):
    page = await abrir("/config")

    async def fake(route):
        await route.fulfill(json={"ok": True, "mensagens": 2})
    await page.route("**/api/livepix/testar", fake)
    await page.click('.it[data-v="livepix"]')
    await page.fill("#lp_id", "x-id")
    await page.fill("#lp_sec", "x-sec")
    await page.click("#lp_btn")
    await expect(page.locator("#lp_msg")).to_contain_text("falta salvar")
    await page.click("#lp_msg button:has-text('Salvar agora')")
    await expect(page.locator("#barra")).to_be_hidden()
    assert settings.load_secrets()["livepix"]["client_id"] == "x-id"
    assert page.erros == []
