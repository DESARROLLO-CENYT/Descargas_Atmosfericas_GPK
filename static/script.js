let currentMap = null;
let currentData = null; // Store fetched data

// Cual de las dos vistas (mapa o datos) esta a la vista. Lo define el conmutador
// de pestanas; por defecto se entra por el mapa.
let vistaActiva = () => 'mapa';

// Puente para que renderDashboard pueda refrescar el calendario cuando cambian
// los filtros de ubicacion; lo define el conmutador de vistas al inicializarse
let recargarCalendario = null;

// Mismo puente para el Simulador de Tormenta
let recargarSimulador = null;

// Texto que viene de los Excel: se escapa antes de armar HTML a mano
function escapar(texto) {
    const div = document.createElement('div');
    div.textContent = texto == null ? '' : texto;
    return div.innerHTML;
}

// Todo lo que muestra la vista de datos con el analisis ya calculado
function renderVistaDatos(data) {
    renderGraficaCorriente(data);
    renderTabla(data);
}
let layers = {
    structures: null,
    radii: null,
    strikes: null,
    destacado: null,
    calorFondo: null
};

document.addEventListener('DOMContentLoaded', () => {
    
    // ---- Filtros de ubicacion ----
    // La jerarquia es campo > locacion > portico > estructura, pero se puede
    // recorrer en los dos sentidos: elegir un nivel cualquiera rellena solo los
    // de arriba y acota los de abajo. Funciona porque el cruce es un arbol
    // estricto (cada portico cuelga de una sola locacion y cada locacion de un
    // solo campo), asi que subir por el nunca es ambiguo.
    const NIVELES = ['campo', 'locacion', 'portico'];

    let filasUbicacion = [];      // [{campo, locacion, portico}], una por circuito
    let catalogoEstructuras = []; // [{tag, portico, locacion, campo}]

    const filtroCampo = document.getElementById('filtroCampo');
    const filtroLocacion = document.getElementById('filtroLocacion');
    const filtroPortico = document.getElementById('filtroPortico');
    const selectPorNivel = { campo: filtroCampo, locacion: filtroLocacion, portico: filtroPortico };

    const inputEstructura = document.getElementById('filtroEstructura');
    const listaEstructuras = document.getElementById('listaEstructuras');
    const notaEstructura = document.getElementById('notaEstructura');
    const btnLimpiarEstructura = document.getElementById('limpiarEstructura');
    const cajaBuscador = document.getElementById('buscadorEstructura');

    let estructuraSeleccionada = '';
    let porticosSinUbicacion = [];

    async function loadFiltros() {
        try {
            const response = await fetch('/api/filtros');
            if (!response.ok) return;
            const data = await response.json();
            if (data.error) throw new Error(data.error);

            filasUbicacion = [];
            Object.keys(data.filtros || {}).forEach(campo => {
                Object.keys(data.filtros[campo]).forEach(locacion => {
                    data.filtros[campo][locacion].forEach(portico => {
                        filasUbicacion.push({ campo, locacion, portico });
                    });
                });
            });

            catalogoEstructuras = data.estructuras || [];
            porticosSinUbicacion = data.sin_asignar || [];

            pintarSelects({ campo: '', locacion: '', portico: '' });
            pintarNota();
        } catch (error) {
            console.error("Error cargando filtros:", error);
        }
    }

    function seleccionActual() {
        return {
            campo: filtroCampo ? filtroCampo.value : '',
            locacion: filtroLocacion ? filtroLocacion.value : '',
            portico: filtroPortico ? filtroPortico.value : ''
        };
    }

    // Las opciones de cada nivel salen de lo que ya eligieron sus ancestros; si
    // no hay ninguno seleccionado, se listan todas (por eso se puede arrancar
    // directamente por el portico)
    function opcionesDe(nivel, sel) {
        const i = NIVELES.indexOf(nivel);
        const valores = filasUbicacion
            .filter(f => NIVELES.slice(0, i).every(n => !sel[n] || f[n] === sel[n]))
            .map(f => f[nivel]);
        return [...new Set(valores)].sort((a, b) => a.localeCompare(b, 'es'));
    }

    function pintarSelects(sel) {
        NIVELES.forEach(nivel => {
            const select = selectPorNivel[nivel];
            if (!select) return;
            select.innerHTML = '<option value="">TODOS</option>';
            const opciones = opcionesDe(nivel, sel);
            // Un circuito que no figura en el maestro no sale de la jerarquia,
            // pero si el usuario llego a el por una estructura hay que poder
            // verlo seleccionado en vez de dejar el select en blanco
            if (sel[nivel] && !opciones.includes(sel[nivel])) opciones.push(sel[nivel]);
            opciones.forEach(valor => {
                const opt = document.createElement('option');
                opt.value = valor;
                opt.textContent = valor;
                select.appendChild(opt);
            });
            select.value = sel[nivel] || '';
        });
    }

    // Al tocar un nivel: hacia arriba se autocompleta (hay una sola respuesta
    // posible) y hacia abajo se suelta lo que ya no cuelga de la nueva eleccion
    function propagarCambio(nivelTocado) {
        const sel = seleccionActual();
        const iTocado = NIVELES.indexOf(nivelTocado);

        if (sel[nivelTocado]) {
            const fila = filasUbicacion.find(f => f[nivelTocado] === sel[nivelTocado]);
            if (fila) {
                for (let i = 0; i < iTocado; i++) sel[NIVELES[i]] = fila[NIVELES[i]];
            }
            for (let i = iTocado + 1; i < NIVELES.length; i++) {
                const n = NIVELES[i];
                if (sel[n] && !opcionesDe(n, sel).includes(sel[n])) sel[n] = '';
            }
        } else {
            // Vaciar un nivel vacia tambien los que dependen de el: dejarlos
            // colgando daria un filtro mas estrecho de lo que el usuario cree
            for (let i = iTocado + 1; i < NIVELES.length; i++) sel[NIVELES[i]] = '';
        }

        pintarSelects(sel);
        // La estructura elegida es la que manda mientras siga siendo compatible;
        // si el usuario la contradice desde arriba, se suelta
        if (estructuraSeleccionada && !estructurasDelFiltro(sel).some(e => e.id === estructuraSeleccionada)) {
            fijarEstructura('', { recalcular: false });
        }
        pintarNota();
    }

    NIVELES.forEach(nivel => {
        const select = selectPorNivel[nivel];
        if (select) select.addEventListener('change', () => propagarCambio(nivel));
    });

    // ---- Buscador de estructura especifica ----
    // Son ~730 tags: se escribe y se filtra, resaltando la parte que coincide

    function estructurasDelFiltro(sel) {
        return catalogoEstructuras.filter(e =>
            (!sel.campo || e.campo === sel.campo) &&
            (!sel.locacion || e.locacion === sel.locacion) &&
            (!sel.portico || e.portico === sel.portico)
        );
    }

    // Guarda a que posicion del texto original corresponde cada caracter ya
    // normalizado, para poder resaltar sobre el tag tal cual esta escrito.
    // La busqueda exige que lo tecleado aparezca seguido, pero ignorando
    // mayusculas, guiones, espacios y tildes.
    function indexar(texto) {
        let norm = '';
        const mapa = [];
        for (let i = 0; i < texto.length; i++) {
            // NFD separa la letra de su tilde. Sin esto "ó" se caia entera al
            // filtrar por A-Z y "derivacion" no encontraba "Derivación"
            for (const parte of texto[i].normalize('NFD')) {
                const c = parte.toUpperCase();
                if (c >= 'A' && c <= 'Z' || c >= '0' && c <= '9') {
                    norm += c;
                    mapa.push(i);
                }
            }
        }
        return { norm, mapa };
    }

    function normalizar(texto) {
        return indexar(texto).norm;
    }

    function resaltar(texto, desde, hasta) {
        return escapar(texto.slice(0, desde)) +
               '<mark>' + escapar(texto.slice(desde, hasta + 1)) + '</mark>' +
               escapar(texto.slice(hasta + 1));
    }

    const MAX_RESULTADOS = 60;

    function buscarEstructuras(consulta) {
        const universo = estructurasDelFiltro(seleccionActual());
        const q = normalizar(consulta);
        if (!q) {
            return { total: universo.length, items: universo.slice(0, MAX_RESULTADOS).map(e => ({ est: e, html: escapar(e.tag) })) };
        }

        const encontrados = [];
        universo.forEach(est => {
            const idx = indexar(est.tag);
            const pos = idx.norm.indexOf(q);
            if (pos === -1) return;
            encontrados.push({
                est,
                pos,
                html: resaltar(est.tag, idx.mapa[pos], idx.mapa[pos + q.length - 1])
            });
        });

        // Los que empiezan por lo tecleado van primero: es lo que uno espera al
        // escribir "JCB-TIE" buscando la serie JCB-TIE##
        encontrados.sort((a, b) => a.pos - b.pos || a.est.tag.localeCompare(b.est.tag, 'es'));
        return { total: encontrados.length, items: encontrados.slice(0, MAX_RESULTADOS) };
    }

    let indiceActivo = -1;
    let resultadosVisibles = [];

    function cerrarLista() {
        if (!listaEstructuras) return;
        listaEstructuras.hidden = true;
        inputEstructura.setAttribute('aria-expanded', 'false');
        indiceActivo = -1;
    }

    function abrirLista(consulta) {
        if (!listaEstructuras) return;
        const { total, items } = buscarEstructuras(consulta);
        resultadosVisibles = items;
        indiceActivo = -1;
        listaEstructuras.innerHTML = '';

        if (items.length === 0) {
            const li = document.createElement('li');
            li.className = 'op-vacio';
            li.textContent = 'Ninguna estructura coincide';
            listaEstructuras.appendChild(li);
        } else {
            items.forEach((r, i) => {
                const li = document.createElement('li');
                li.setAttribute('role', 'option');
                const ruta = r.est.campo
                    ? `${r.est.campo} · ${r.est.locacion} · ${r.est.portico}`
                    : `${r.est.portico} · sin campo asignado`;
                li.innerHTML = `<span class="op-tag">${r.html}</span><span class="op-ruta">${escapar(ruta)}</span>`;
                // mousedown y no click: el blur del input llega antes que el click
                // y cerraria la lista sin llegar a seleccionar
                li.addEventListener('mousedown', ev => {
                    ev.preventDefault();
                    fijarEstructura(r.est.id);
                });
                li.addEventListener('mouseenter', () => marcarActiva(i));
                listaEstructuras.appendChild(li);
            });

            if (total > items.length) {
                const li = document.createElement('li');
                li.className = 'op-mas';
                li.textContent = `y ${(total - items.length).toLocaleString('es-CO')} más — sigue escribiendo para acotar`;
                listaEstructuras.appendChild(li);
            }
        }

        listaEstructuras.hidden = false;
        inputEstructura.setAttribute('aria-expanded', 'true');
        ubicarLista();
    }

    // El sidebar tiene scroll propio y recorta lo que se salga: si el buscador
    // quedo abajo, la lista se despliega hacia arriba en vez de perderse
    function ubicarLista() {
        const contenedor = document.querySelector('.sidebar');
        if (!contenedor) return;
        const caja = inputEstructura.getBoundingClientRect();
        const limite = contenedor.getBoundingClientRect();
        const alto = listaEstructuras.offsetHeight;
        const cabeAbajo = caja.bottom + 4 + alto <= limite.bottom;
        listaEstructuras.classList.toggle('arriba', !cabeAbajo && caja.top - 4 - alto >= limite.top);
    }

    function marcarActiva(i) {
        indiceActivo = i;
        [...listaEstructuras.querySelectorAll('li[role="option"]')].forEach((li, j) => {
            li.classList.toggle('activa', j === i);
            if (j === i) li.scrollIntoView({ block: 'nearest' });
        });
    }

    // El identificador y el texto visible dejaron de ser lo mismo: un portico de
    // derivacion se llama "PORT" igual que otros veinte, y lo que lo distingue
    // es su locacion. Se selecciona por id y se muestra el tag.
    function estructuraPorId(id) {
        return catalogoEstructuras.find(e => e.id === id);
    }

    // Fijar una estructura completa la jerarquia hacia arriba de una sola vez:
    // es el caso que motivo todo esto, poder entrar por el tag sin saber de
    // antemano a que campo o locacion pertenece
    function fijarEstructura(id, { recalcular = true, limpiarFiltros = false } = {}) {
        estructuraSeleccionada = id || '';
        const est = estructuraSeleccionada ? estructuraPorId(estructuraSeleccionada) : null;

        if (inputEstructura) inputEstructura.value = est ? est.tag : '';
        if (cajaBuscador) cajaBuscador.classList.toggle('activo', !!estructuraSeleccionada);
        if (btnLimpiarEstructura) btnLimpiarEstructura.hidden = !estructuraSeleccionada;
        cerrarLista();

        if (est) {
            pintarSelects({ campo: est.campo, locacion: est.locacion, portico: est.portico });
            // Elegir una estructura concreta es el filtro mas especifico que
            // hay: un filtro de proteccion anterior solo puede contradecirlo y
            // dejar el mapa vacio, asi que se suelta.
            const todos = radiosProteccion.find(r => !r.value);
            if (todos && !todos.checked) {
                todos.checked = true;
                sincronizarSubfiltros();
            }
        } else {
            ocultarDetalle();
            // Soltar la estructura devuelve todo a "Todos". Dejar puestos los
            // filtros que ella misma habia rellenado hacia que la busqueda
            // siguiente arrancara acotada sin ninguna senal de por que.
            if (limpiarFiltros) {
                pintarSelects({ campo: '', locacion: '', portico: '' });
                const todos = radiosProteccion.find(r => !r.value);
                if (todos) todos.checked = true;
                sincronizarSubfiltros();
            }
        }

        pintarNota();
        if (recalcular) ejecutarAnalisis();
    }

    function pintarNota() {
        if (!notaEstructura) return;
        if (estructuraSeleccionada) {
            const est = estructuraPorId(estructuraSeleccionada);
            if (est && !est.campo) {
                notaEstructura.textContent = `Su circuito (${est.portico}) no tiene campo ni locación en el maestro.`;
                notaEstructura.className = 'buscador-nota aviso';
                return;
            }
            notaEstructura.textContent = 'Analizando solo esta estructura.';
            notaEstructura.className = 'buscador-nota';
            return;
        }
        const disponibles = estructurasDelFiltro(seleccionActual()).length;
        const huerfanas = porticosSinUbicacion.reduce((n, p) => n + p.estructuras, 0);
        let texto = `${disponibles.toLocaleString('es-CO')} estructuras disponibles.`;
        if (huerfanas > 0 && !seleccionActual().campo) {
            texto += ` ${huerfanas} sin campo asignado.`;
        }
        notaEstructura.textContent = texto;
        notaEstructura.className = 'buscador-nota';
    }

    if (inputEstructura) {
        inputEstructura.addEventListener('input', () => {
            // Escribir invalida la seleccion anterior: el texto pasa a ser una
            // busqueda en curso, no un filtro aplicado. Se compara contra el tag
            // y no contra el id, que en un portico no es lo que se ve escrito.
            const tagElegido = estructuraSeleccionada
                ? (estructuraPorId(estructuraSeleccionada) || {}).tag
                : '';
            if (estructuraSeleccionada && inputEstructura.value !== tagElegido) {
                estructuraSeleccionada = '';
                if (cajaBuscador) cajaBuscador.classList.remove('activo');
                if (btnLimpiarEstructura) btnLimpiarEstructura.hidden = true;
                ocultarDetalle();
            }
            abrirLista(inputEstructura.value);
        });

        inputEstructura.addEventListener('focus', () => abrirLista(inputEstructura.value));
        inputEstructura.addEventListener('blur', () => setTimeout(() => {
            cerrarLista();
            // Texto sin estructura elegida es una busqueda a medias: dejarlo
            // ahi haria creer que hay un filtro aplicado que no existe
            if (!estructuraSeleccionada && inputEstructura.value) {
                inputEstructura.value = '';
                pintarNota();
            }
        }, 120));

        inputEstructura.addEventListener('keydown', ev => {
            if (listaEstructuras.hidden && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
                abrirLista(inputEstructura.value);
                return;
            }
            if (ev.key === 'ArrowDown') {
                ev.preventDefault();
                if (resultadosVisibles.length) marcarActiva((indiceActivo + 1) % resultadosVisibles.length);
            } else if (ev.key === 'ArrowUp') {
                ev.preventDefault();
                if (resultadosVisibles.length) marcarActiva((indiceActivo - 1 + resultadosVisibles.length) % resultadosVisibles.length);
            } else if (ev.key === 'Enter') {
                ev.preventDefault();
                // Sin nada resaltado, un unico resultado es eleccion inequivoca
                const elegido = indiceActivo >= 0 ? resultadosVisibles[indiceActivo]
                              : (resultadosVisibles.length === 1 ? resultadosVisibles[0] : null);
                if (elegido) fijarEstructura(elegido.est.id);
            } else if (ev.key === 'Escape') {
                cerrarLista();
            }
        });
    }

    if (btnLimpiarEstructura) {
        btnLimpiarEstructura.addEventListener('click', () => fijarEstructura('', { limpiarFiltros: true }));
    }

    // Inicializar filtros. Se guarda la promesa porque el analisis de arranque
    // no puede salir antes de que existan los filtros y las fechas por defecto
    const filtrosListos = loadFiltros();

    const inputInicio = document.getElementById('fechaInicio');
    const inputFin = document.getElementById('fechaFin');

    // Convierte un Date del calendario a YYYY-MM-DD en hora local.
    // toISOString() no sirve acá: pasa a UTC y en Colombia (UTC-5) devuelve
    // el día anterior para cualquier fecha del calendario.
    function aISOLocal(d) {
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        return `${d.getFullYear()}-${mm}-${dd}`;
    }

    // Rango de fechas del dataset, compartido por el calendario y el simulador
    // para no pedir /api/rango-fechas dos veces
    let rangoFechas = null;

    // Dias con impacto dentro del radio (rojo en el calendario del sidebar).
    // Depende del radio y los filtros: lo actualiza actualizarDiasRadio(). Se
    // guardan aca (y no local en initCalendarios) para poder redibujar los
    // selectores cuando el conjunto cambia.
    let diasConRadio = new Set();
    let fpInicio = null, fpFin = null;

    async function actualizarDiasRadio() {
        try {
            const resp = await fetch('/api/dias-radio', { method: 'POST', body: cuerpoFiltros() });
            if (!resp.ok) return;
            const data = await resp.json();
            diasConRadio = new Set(data.dias_con_radio || []);
            if (fpInicio) fpInicio.redraw();
            if (fpFin) fpFin.redraw();
        } catch (error) {
            console.error('No se pudo actualizar los días con impacto:', error);
        }
    }

    // Calendarios: rojo los días con impacto dentro del radio, verde los que
    // tienen datos pero sin impacto en las estructuras filtradas. Por defecto
    // arrancan en el último día con datos (no en "hoy", que casi siempre cae
    // fuera del rango cargado)
    async function initCalendarios() {
        if (!inputInicio || !inputFin) return;

        let rango;
        try {
            const response = await fetch('/api/rango-fechas');
            if (!response.ok) throw new Error('No se pudo leer el rango de fechas');
            rango = await response.json();
            rangoFechas = rango;
            poblarSelectoresCalendario(rango);
            if (rango.error || !rango.max) throw new Error(rango.error || 'Rango vacío');
        } catch (error) {
            // Sin rango se dejan los input date nativos, que siguen siendo usables
            console.error("Error cargando rango de fechas:", error);
            const legend = document.getElementById('dateLegend');
            if (legend) legend.style.display = 'none';
            return;
        }

        // Si flatpickr no cargó (CDN caído) los input date nativos siguen vivos
        if (typeof flatpickr === 'undefined') {
            inputInicio.value = rango.max;
            inputFin.value = rango.max;
            const legend = document.getElementById('dateLegend');
            if (legend) legend.style.display = 'none';
            return;
        }

        const diasConDatos = new Set(rango.dias_con_datos);

        if (flatpickr.l10ns && flatpickr.l10ns.es) flatpickr.localize(flatpickr.l10ns.es);

        // flatpickr necesita inputs de texto: sobre type="date" el navegador
        // abriría además su propio selector nativo
        inputInicio.type = 'text';
        inputFin.type = 'text';

        const configBase = {
            dateFormat: 'Y-m-d',
            altInput: true,
            altFormat: 'd/m/Y',
            minDate: rango.min,
            maxDate: rango.max,
            // Flotante (fuera del sidebar) a proposito: anclado adentro, al
            // abrirse hacia crecer el alto del panel y aparecia una barra de
            // desplazamiento que rompia los filtros. Ademas, en este modo
            // flatpickr solo lo abre hacia arriba cuando no hay sitio abajo,
            // asi funciona igual en una laptop que en un monitor grande
            static: false,
            appendTo: document.body,
            onReady: (_sel, _str, fp) => {
                fp.calendarContainer.classList.add('cal-filtro');
            },
            // Se dispara justo antes de que la libreria calcule la posicion:
            // el ancho se iguala al del campo para que se lea como una
            // extension del filtro y no como una ventana suelta
            onPreCalendarPosition: (_sel, _str, fp) => {
                const campo = fp.altInput || fp.input;
                fp.calendarContainer.style.width = campo.offsetWidth + 'px';
            },
            onDayCreate: (dObj, dStr, fp, dayElem) => {
                const iso = aISOLocal(dayElem.dateObj);
                // Rojo (impacto en el radio) tiene prioridad sobre verde (datos)
                if (diasConRadio.has(iso)) dayElem.classList.add('con-radio');
                else if (diasConDatos.has(iso)) dayElem.classList.add('con-datos');
            }
        };

        fpInicio = flatpickr(inputInicio, Object.assign({}, configBase, {
            defaultDate: rango.max,
            onChange: ([d]) => { if (d) fpFin.set('minDate', d); }
        }));

        fpFin = flatpickr(inputFin, Object.assign({}, configBase, {
            defaultDate: rango.max,
            onChange: ([d]) => { if (d) fpInicio.set('maxDate', d); }
        }));

        // Al flotar sobre el tablero, el calendario ya no viaja con el sidebar:
        // si este llegara a desplazarse con uno abierto quedaria descolgado del
        // campo, asi que se cierra. Es un caso de borde (los filtros entran sin
        // scroll), pero mas vale cerrarlo que dejarlo apuntando a otro lado
        const cerrarCalendarios = () => {
            if (fpInicio && fpInicio.isOpen) fpInicio.close();
            if (fpFin && fpFin.isOpen) fpFin.close();
        };
        const sidebar = document.querySelector('.sidebar');
        if (sidebar) sidebar.addEventListener('scroll', cerrarCalendarios, { passive: true });
        window.addEventListener('scroll', cerrarCalendarios, { passive: true });
    }

    const calendariosListos = initCalendarios();

    // UI Elements
    const form = document.getElementById('uploadForm');
    const fileDescargas = document.getElementById('fileDescargas');
    const filePostes = document.getElementById('filePostes');
    // Navegación desde Pantalla de Inicio
    const btnIniciarApp = document.getElementById('btnIniciarApp');
    if (btnIniciarApp) {
        btnIniciarApp.addEventListener('click', async () => {
            document.getElementById('introScreen').style.display = 'none';
            const appContainer = document.getElementById('appContainer');
            appContainer.style.display = 'flex';
            // Trigger a resize event to ensure Leaflet maps render correctly if they were hidden
            window.dispatchEvent(new Event('resize'));

            // Entrar a un tablero vacio no dice nada: se analiza de una toda la
            // red con el rango que dejan puesto los calendarios (el ultimo dia
            // con datos). Hay que esperarlos o saldria una peticion sin fechas.
            await Promise.all([filtrosListos, calendariosListos]);
            ejecutarAnalisis();
        });
    }

    const uploadForm = document.getElementById('uploadForm');
    const btnSubmit = document.getElementById('btnSubmit');
    const spinner = document.getElementById('spinner');
    const btnText = btnSubmit.querySelector('span');
    const statusMessage = document.getElementById('statusMessage');

    const dashboardContent = document.getElementById('dashboardContent');

    // Drag and Drop (Removido para usar archivos locales)


    // Los filtros de ubicacion recalculan solos al cambiar; el boton queda
    // para los parametros de analisis, donde el usuario suele encadenar
    // varios ajustes antes de querer el resultado.
    const selectsUbicacion = [filtroCampo, filtroLocacion, filtroPortico].filter(Boolean);
    const radiosProteccion = [...document.querySelectorAll('input[name="filtroProteccion"]')];
    const subDps = document.getElementById('subDps');
    const subDsd = document.getElementById('subDsd');
    const subfiltros = [subDps, subDsd].filter(Boolean);
    const cajaProteccion = document.getElementById('filtrosProteccion');
    const controlesUbicacion = [...selectsUbicacion, inputEstructura, btnLimpiarEstructura,
                                ...radiosProteccion, ...subfiltros].filter(Boolean);

    function modoMapa() {
        const marcado = document.querySelector('input[name="mapMode"]:checked');
        return marcado ? marcado.value : 'general';
    }

    // El filtro de proteccion pertenece al Mapa General: el de calor tiene sus
    // propios controles y ahi se analiza siempre la red completa.
    function proteccionActual() {
        if (modoMapa() === 'heatmap') return '';
        const principal = radiosProteccion.find(r => r.checked);
        if (!principal || !principal.value) return '';
        // Los subfiltros solo afinan dentro de las protegidas
        if (principal.value === 'protegidas') {
            if (subDps && subDps.checked) return 'dps';
            if (subDsd && subDsd.checked) return 'dsd';
        }
        return principal.value;
    }

    // Toda esta fila (filtros de proteccion y escala de corriente) describe el
    // Mapa General. El de calor tiene su propia leyenda con sus propios
    // controles, asi que ahi la fila entera sobra.
    function sincronizarLeyendaMapa() {
        const fila = document.querySelector('.map-legend');
        if (fila) fila.style.display = modoMapa() === 'heatmap' ? 'none' : '';
    }

    // Los subfiltros solo tienen sentido dentro de DPS/DSD. Con "Todos" o con
    // "Estructuras" se apagan: en el primero no acotan nada y en el segundo
    // pedirian DPS dentro de las que justamente no tienen proteccion.
    function sincronizarSubfiltros() {
        const principal = radiosProteccion.find(r => r.checked);
        const habilitados = !!(principal && principal.value === 'protegidas');
        subfiltros.forEach(s => {
            s.disabled = !habilitados;
            if (!habilitados) s.checked = false;
        });
        if (cajaProteccion) cajaProteccion.classList.toggle('sub-inactivos', !habilitados);
    }

    // Un cambio rapido de filtros puede dejar dos peticiones en vuelo. Se
    // numeran para descartar la respuesta vieja si llega despues de la nueva.
    let peticionActual = 0;

    // Con que proteccion se pidieron los datos que hay en pantalla, para saber
    // si al cambiar de modo de mapa hace falta volver a consultar
    let ultimaProteccionEnviada = '';

    function setCargando(activo) {
        btnSubmit.disabled = activo;
        btnText.textContent = activo ? 'Calculando...' : 'Analizar datos';
        spinner.style.display = activo ? 'block' : 'none';

        // Los filtros quedan bloqueados mientras se calcula: tocarlos a media
        // peticion solo genera respuestas que se descartan
        controlesUbicacion.forEach(c => { c.disabled = activo; });
        if (activo) {
            cerrarLista();
        } else {
            // Rehabilitar en bloque pisaria a los subfiltros, que dependen de
            // si DPS/DSD esta elegido y no de si hay un calculo en curso
            sincronizarSubfiltros();
        }
    }

    // Los filtros se arman en un solo lugar: el analisis y la descarga del
    // informe usan esto mismo, asi el Excel no puede quedar describiendo un
    // recorte distinto al que se ve en pantalla
    function cuerpoFiltros() {
        const { campo, locacion, portico } = seleccionActual();
        const formData = new FormData();
        formData.append('radio_busqueda_metros', document.getElementById('radioBusqueda').value);

        const fechaInicio = document.getElementById('fechaInicio').value;
        const fechaFin = document.getElementById('fechaFin').value;
        if (fechaInicio) formData.append('fecha_inicio', fechaInicio);
        if (fechaFin) formData.append('fecha_fin', fechaFin);
        if (campo) formData.append('filtro_campo', campo);
        if (locacion) formData.append('filtro_locacion', locacion);
        if (portico) formData.append('filtro_portico', portico);
        if (estructuraSeleccionada) formData.append('filtro_estructura', estructuraSeleccionada);

        const proteccion = proteccionActual();
        if (proteccion) formData.append('filtro_proteccion', proteccion);
        return formData;
    }

    async function ejecutarAnalisis() {
        const miPeticion = ++peticionActual;
        statusMessage.className = 'status-message';
        statusMessage.textContent = '';

        const formData = cuerpoFiltros();
        ultimaProteccionEnviada = proteccionActual();

        // El marcado rojo del calendario depende del radio y los filtros: se
        // recalcula en paralelo, sin bloquear el analisis principal
        actualizarDiasRadio();

        setCargando(true);

        try {
            const response = await fetch('/api/procesar', {
                method: 'POST',
                body: formData
            });

            // Quedo obsoleta: ya salio otra peticion mas nueva
            if (miPeticion !== peticionActual) return;

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.message || 'Error en procesamiento');
            }

            const data = await response.json();
            if (miPeticion !== peticionActual) return;

            currentData = data; // Guardar estado global

            // Switch views
            if (dashboardContent) dashboardContent.style.display = 'flex';

            // Mostrar main content si estaba oculto
            const mainContent = document.getElementById('mainContent');
            if (mainContent && mainContent.style.display === 'none') {
                mainContent.style.display = 'flex';
            } 
            renderDashboard(data);
            // El resultado del analisis lo cuentan las tarjetas de KPI: repetirlo
            // en un cartel bajo los filtros era ruido. Solo se avisa lo que las
            // tarjetas no pueden mostrar (un problema con los datos o la peticion)
            if (data.aviso) showStatus(data.aviso);


        } catch (error) {
            if (miPeticion !== peticionActual) return;
            console.error(error);
            showStatus(error.message);
        } finally {
            // Solo la peticion vigente devuelve la UI a su estado normal, para
            // que una respuesta vieja no apague el spinner de la que sigue viva
            if (miPeticion === peticionActual) setCargando(false);
        }
    }

    // El boton aplica los parametros de analisis (fechas y radio)
    form.addEventListener('submit', (e) => {
        e.preventDefault();
        ejecutarAnalisis();
    });

    // Los filtros de ubicacion se aplican solos
    selectsUbicacion.forEach(sel => sel.addEventListener('change', ejecutarAnalisis));

    // El de proteccion tambien: recorta estructuras igual que los de ubicacion
    radiosProteccion.forEach(radio => radio.addEventListener('change', () => {
        sincronizarSubfiltros();
        ejecutarAnalisis();
    }));

    // Son casillas, no radios, para poder apagarlas y volver a ver las dos
    // categorias; pero solo una a la vez, porque "las dos juntas" ya es DPS/DSD
    subfiltros.forEach(sub => sub.addEventListener('change', () => {
        if (sub.checked) subfiltros.forEach(otro => { if (otro !== sub) otro.checked = false; });
        ejecutarAnalisis();
    }));

    sincronizarSubfiltros();
    sincronizarLeyendaMapa();

    // ---- Cambio de vista Mapa / Datos ----
    // Las dos comparten filtros y KPIs; cambia solo el cuerpo. La grafica se
    // dibuja al entrar a Datos porque Chart.js necesita que el lienzo tenga
    // tamano: sobre un contenedor oculto sale de 0 px.
    const VISTAS = {
        mapa: { tab: document.getElementById('tabMapa'), panel: document.getElementById('vistaMapa') },
        datos: { tab: document.getElementById('tabDatos'), panel: document.getElementById('vistaDatos') },
        calendario: { tab: document.getElementById('tabCalendario'), panel: document.getElementById('vistaCalendario') },
        simulador: { tab: document.getElementById('tabSimulador'), panel: document.getElementById('vistaSimulador') }
    };
    let vistaEnPantalla = 'mapa';

    const filaKpis = document.querySelector('.kpi-grid');

    function mostrarVista(cual) {
        // Al salir del simulador se detiene el bucle de animacion: seguir
        // dibujando sobre un canvas oculto solo gasta bateria
        if (cual !== 'simulador') {
            if (typeof pausarSim === 'function') pausarSim();
            if (typeof detenerBucleSim === 'function') detenerBucleSim();
        }

        vistaEnPantalla = cual;
        // Los KPIs describen el rango de fechas del sidebar, no el mes del
        // calendario ni el dia del simulador: dejarlos visibles ahi mostraba
        // dos periodos distintos en la misma pantalla como si fueran el mismo
        if (filaKpis) filaKpis.hidden = cual === 'calendario' || cual === 'simulador';

        Object.entries(VISTAS).forEach(([nombre, v]) => {
            const activa = nombre === cual;
            if (v.panel) v.panel.hidden = !activa;
            if (v.tab) {
                v.tab.classList.toggle('activa', activa);
                v.tab.setAttribute('aria-selected', String(activa));
            }
        });

        if (cual === 'datos') {
            if (currentData) renderVistaDatos(currentData);
        } else if (cual === 'calendario') {
            cargarCalendario();
        } else if (cual === 'simulador') {
            initSimMap();
            initSimCanvas();
            simMap.invalidateSize();
            dimensionarSimCanvas();
            dibujarEstructurasSim();
            iniciarBucleSim();
            // Se recarga solo si el rango del sidebar cambio desde lo ya cargado
            const ini = document.getElementById('fechaInicio').value;
            const fin = document.getElementById('fechaFin').value;
            const yaCargado = datosSimulador && datosSimulador.inicio === ini && datosSimulador.fin === fin;
            if (!yaCargado) cargarSimulador();
        } else if (currentMap) {
            // El mapa estuvo oculto y Leaflet no recalcula solo su tamano
            currentMap.invalidateSize();
        }
    }

    Object.entries(VISTAS).forEach(([nombre, v]) => {
        if (v.tab) v.tab.addEventListener('click', () => mostrarVista(nombre));
    });

    // ---- Calendario ----
    const calAnio = document.getElementById('calAnio');
    const calMes = document.getElementById('calMes');
    let calPeticion = 0;

    function poblarSelectoresCalendario(rango) {
        if (!calAnio || !calMes) return;
        const desde = rango ? Number(rango.min.slice(0, 4)) : new Date().getFullYear();
        const hasta = rango ? Number(rango.max.slice(0, 4)) : desde;

        calAnio.innerHTML = '';
        for (let a = hasta; a >= desde; a--) {
            calAnio.appendChild(new Option(a, a));
        }
        calMes.innerHTML = '';
        MESES.forEach((m, i) => calMes.appendChild(new Option(m, i + 1)));

        // Arranca en el ultimo mes con datos, que es donde el usuario espera
        // encontrar algo; el mes en curso suele estar vacio
        if (rango) {
            calAnio.value = rango.max.slice(0, 4);
            calMes.value = String(Number(rango.max.slice(5, 7)));
        }
    }

    async function cargarCalendario() {
        if (!calAnio || !calAnio.value) return;
        const mia = ++calPeticion;
        const grilla = document.getElementById('calGrilla');
        if (grilla) grilla.classList.add('cargando');

        try {
            const cuerpo = cuerpoFiltros();
            // El calendario tiene su propio mes: las fechas del sidebar no aplican
            cuerpo.delete('fecha_inicio');
            cuerpo.delete('fecha_fin');
            cuerpo.append('anio', calAnio.value);
            cuerpo.append('mes', calMes.value);

            const resp = await fetch('/api/calendario', { method: 'POST', body: cuerpo });
            if (mia !== calPeticion) return;   // llego tarde, ya salio otra
            if (!resp.ok) throw new Error('No se pudo cargar el calendario');
            renderCalendario(await resp.json());
        } catch (error) {
            console.error(error);
            showStatus(error.message);
        } finally {
            if (mia === calPeticion && grilla) grilla.classList.remove('cargando');
        }
    }

    [calAnio, calMes].forEach(s => s && s.addEventListener('change', cargarCalendario));
    recargarCalendario = cargarCalendario;

    // Navegacion mes a mes, saltando de anio cuando corresponde
    function moverMes(paso) {
        if (!calAnio || !calMes) return;
        let m = Number(calMes.value) + paso;
        let a = Number(calAnio.value);
        if (m < 1) { m = 12; a--; }
        if (m > 12) { m = 1; a++; }
        if (![...calAnio.options].some(o => Number(o.value) === a)) return;  // fuera del rango con datos
        calAnio.value = a;
        calMes.value = m;
        cargarCalendario();
    }
    const btnAnterior = document.getElementById('calAnterior');
    const btnSiguiente = document.getElementById('calSiguiente');
    if (btnAnterior) btnAnterior.addEventListener('click', () => moverMes(-1));
    if (btnSiguiente) btnSiguiente.addEventListener('click', () => moverMes(1));

    // La mini tarjeta del dia vive fuera de este cierre (mas abajo en el
    // archivo), pero saltar al Mapa si necesita ejecutarAnalisis y mostrarVista,
    // que son locales aca
    const btnVerEnMapa = document.getElementById('calTjMapa');
    if (btnVerEnMapa) {
        btnVerEnMapa.addEventListener('click', () => {
            const fecha = document.getElementById('calTarjeta')?.dataset.fecha;
            if (!fecha) return;
            fijarRangoFechas(fecha);
            mostrarVista('mapa');
            ejecutarAnalisis();
        });
    }

    // ---- Simulador de tormenta ----
    // El periodo a simular sale del rango de fechas del sidebar (Parametros de
    // analisis): un dia, una semana, un mes, un anio o todo el historial. No
    // tiene selector propio; se recarga cuando cambian esas fechas o los filtros.
    let simPeticion = 0;

    async function cargarSimulador() {
        const inicio = document.getElementById('fechaInicio').value;
        const fin = document.getElementById('fechaFin').value;
        if (!inicio || !fin) return;
        const mia = ++simPeticion;
        try {
            // cuerpoFiltros ya incluye fecha_inicio, fecha_fin, radio y filtros
            const resp = await fetch('/api/simulador', { method: 'POST', body: cuerpoFiltros() });
            if (mia !== simPeticion) return;
            if (!resp.ok) throw new Error('No se pudo cargar el simulador');
            renderSimulador(await resp.json());
        } catch (error) {
            console.error(error);
            showStatus(error.message);
        }
    }
    recargarSimulador = cargarSimulador;

    // Reproduccion: play/pausa, reinicio y velocidad
    const simPlay = document.getElementById('simPlay');
    const simRe = document.getElementById('simReiniciar');
    const simVel = document.getElementById('simVelocidad');
    if (simPlay) simPlay.addEventListener('click', alternarSimPlay);
    if (simRe) simRe.addEventListener('click', reiniciarSim);
    if (simVel) simVel.addEventListener('change', () => {
        // El valor es el multiplicador de velocidad. El remapeo (dia base de
        // 20 s) no cambia; solo se acelera cuanto tiempo real consume
        simVelReproduccion = Number(simVel.value) || 1;
    });

    // ---- Sub-pestanas de tablas ----
    const buscadorTabla = document.getElementById('buscadorTabla');

    document.querySelectorAll('.tabla-tab').forEach(boton => {
        boton.addEventListener('click', () => {
            tablaActual = boton.dataset.tabla;
            document.querySelectorAll('.tabla-tab').forEach(b => b.classList.toggle('activa', b === boton));
            // Cada tabla recuerda su propia busqueda
            if (buscadorTabla) buscadorTabla.value = busquedaPorTabla[tablaActual] || '';
            if (currentData) renderTabla(currentData);
        });
    });

    if (buscadorTabla) {
        buscadorTabla.addEventListener('input', () => {
            busquedaPorTabla[tablaActual] = buscadorTabla.value;
            if (currentData) renderTabla(currentData);
        });
    }

    // ---- Descarga del informe ----
    // Se manda por POST con los mismos filtros que la pantalla, asi el Excel
    // refleja exactamente lo que se esta viendo y no el universo completo
    const btnExportar = document.getElementById('btnExportar');
    if (btnExportar) {
        btnExportar.addEventListener('click', async () => {
            const original = btnExportar.textContent;
            btnExportar.disabled = true;
            btnExportar.textContent = 'Generando…';
            try {
                const respuesta = await fetch('/api/exportar', { method: 'POST', body: cuerpoFiltros() });
                if (!respuesta.ok) throw new Error('El servidor no pudo generar el informe');

                // El nombre lo define el backend en Content-Disposition
                const cabecera = respuesta.headers.get('Content-Disposition') || '';
                const coincide = cabecera.match(/filename="(.+?)"/);
                const blob = await respuesta.blob();
                const url = URL.createObjectURL(blob);
                const enlace = document.createElement('a');
                enlace.href = url;
                enlace.download = coincide ? coincide[1] : 'informe.xlsx';
                document.body.appendChild(enlace);
                enlace.click();
                enlace.remove();
                URL.revokeObjectURL(url);
                // Sin confirmacion en el sidebar: la descarga ya la anuncia el
                // navegador y el cartel quedaba lejos del boton que la disparo
            } catch (error) {
                console.error(error);
                showStatus(error.message);
            } finally {
                btnExportar.disabled = false;
                btnExportar.textContent = original;
            }
        });
    }

    // El encabezado se redibuja en cada render, asi que el listener va sobre el
    // contenedor y no sobre cada th
    const cabezaTabla = document.getElementById('tablaCabeza');
    if (cabezaTabla) {
        cabezaTabla.addEventListener('click', ev => {
            const th = ev.target.closest('th');
            if (!th || !th.dataset.clave) return;
            const previo = ordenPorTabla[tablaActual];
            ordenPorTabla[tablaActual] = (previo && previo.clave === th.dataset.clave && previo.dir === 'asc')
                ? { clave: th.dataset.clave, dir: 'desc' }
                : { clave: th.dataset.clave, dir: 'asc' };
            if (currentData) renderTabla(currentData);
        });
    }

    vistaActiva = () => vistaEnPantalla;

    // Map Mode Listeners
    document.querySelectorAll('input[name="mapMode"]').forEach(radio => {
        radio.addEventListener('change', () => {
            // El filtro de proteccion solo rige en el Mapa General, asi que
            // entrar o salir del de calor cambia que estructuras se analizan.
            // Cuando cambia hay que volver a pedir los datos, no solo repintar.
            sincronizarLeyendaMapa();
            if (proteccionActual() !== ultimaProteccionEnviada) {
                ejecutarAnalisis();
            } else if (currentData) {
                renderMap(currentData);
            }
        });
    });

    // El umbral filtra sobre los datos ya cargados: no hace falta volver a
    // consultar al backend, solo repintar el mapa
    const sliderUmbral = document.getElementById('umbralImpactos');
    if (sliderUmbral) {
        sliderUmbral.addEventListener('input', () => {
            pintarValorUmbral(parseInt(sliderUmbral.min, 10));
            if (currentData) renderMap(currentData);
        });
    }

    // La opacidad se aplica por variable CSS: no hace falta repintar el mapa,
    // solo cambiar el estilo del canvas
    const sliderOpacidad = document.getElementById('opacidadCalor');
    if (sliderOpacidad) {
        const aplicarOpacidad = () => {
            const v = parseInt(sliderOpacidad.value, 10);
            document.documentElement.style.setProperty('--opacidad-calor', v / 100);
            document.getElementById('opacidadValor').textContent = `${v} %`;
        };
        sliderOpacidad.addEventListener('input', aplicarOpacidad);
        aplicarOpacidad();
    }

    const topToggle = document.getElementById('topToggle');
    if (topToggle) {
        topToggle.addEventListener('click', () => {
            const panel = document.getElementById('topPanel');
            const plegado = panel.classList.toggle('plegado');
            topToggle.textContent = plegado ? '+' : '−';
            topToggle.setAttribute('aria-expanded', String(!plegado));
        });
    }

    // Solo errores: los resultados los cuentan las tarjetas de KPI, asi que el
    // cartel del sidebar aparece unicamente cuando algo salio mal
    function showStatus(msg) {
        statusMessage.textContent = msg;
        statusMessage.className = 'status-message status-error';
    }
});

// ---- Vista de calendario ----

const MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
               'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'];
const DIAS_SEMANA = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom'];

let datosCalendario = null;

// Dibuja la grilla lunes a domingo del mes que llega del backend.
// El color de cada dia usa la misma escala de criticidad del mapa de calor, y
// se calcula contra el propio mes: asi se aprovecha todo el rango de color
// aunque el mes sea tranquilo, al precio de no poder comparar meses entre si.
function renderCalendario(datos) {
    const grilla = document.getElementById('calGrilla');
    const nota = document.getElementById('calNota');
    if (!grilla || !datos) return;

    datosCalendario = datos;
    const conActividad = datos.dias.filter(d => d.rayos_radio > 0).map(d => d.rayos_radio);
    const min = conActividad.length ? Math.min(...conActividad) : 0;
    const max = conActividad.length ? Math.max(...conActividad) : 0;
    const rango = max - min;

    document.getElementById('calMin').textContent = num(min);
    document.getElementById('calMax').textContent = num(max);

    const r = datos.resumen;
    if (nota) {
        nota.textContent = conActividad.length
            ? `${num(r.total_radio)} descargas dentro de ${num(r.radio)} m · ` +
              `${r.dias_con_actividad} de ${datos.dias_del_mes} días con actividad · ` +
              `${num(r.total_rango)} descargas en toda la región`
            : `Sin descargas dentro del radio este mes · ${num(r.total_rango)} en la región`;
    }

    renderResumenMes(datos, min, max, rango);

    let html = DIAS_SEMANA.map(d => `<div class="cal-cabecera">${d}</div>`).join('');

    // Huecos hasta el primer dia: la grilla arranca en lunes
    for (let i = 0; i < datos.primer_dia_semana; i++) html += '<div class="cal-vacio"></div>';

    datos.dias.forEach(d => {
        const activo = d.rayos_radio > 0;
        // Con un solo dia activo no hay rango que normalizar: va al tope
        const t = activo ? (rango ? (d.rayos_radio - min) / rango : 1) : 0;
        const fondo = activo ? colorCalendario(t) : '';
        // Escala de pastel a intenso: el texto oscuro va sobre el pastel
        // (t bajo), el blanco sobre el rojo intenso (t alto) — al reves que
        // con ESCALA_CALOR, donde lo oscuro caia en el medio del gradiente.
        const claro = activo && t < 0.5;

        // En la casilla va solo el numero de descargas: el resto del detalle lo
        // cuenta la mini tarjeta al pasar el cursor. Sin title tampoco, para que
        // el tooltip del navegador no compita con ella
        html += `<button type="button" class="cal-dia${activo ? ' activo' : ''}${claro ? ' texto-oscuro' : ''}"
                    data-fecha="${d.fecha}"
                    style="${activo ? `background:${fondo}` : ''}">
                    <span class="cal-num">${d.dia}</span>
                    ${activo ? `<span class="cal-datos">
                                    <span class="cal-rayos">${num(d.rayos_radio)}</span>
                                </span>` : ''}
                 </button>`;
    });

    grilla.innerHTML = html;

    // La grilla se rehace entera: la tarjeta fijada apuntaba a una casilla que
    // ya no existe
    cerrarTarjetaDia();
}

// ---- Tarjetas resumen del mes ----
// Llenan el espacio que dejan los KPIs generales, ocultos en esta vista
// porque describen el rango del sidebar y no el mes que se esta mirando

function fechaCorta(fecha) {
    return new Date(`${fecha}T00:00:00`).toLocaleDateString('es-CO', { day: 'numeric', month: 'short' });
}

// La estructura impactada en mas dias distintos del mes. Se cuenta por dias y
// no por descargas porque el backend manda la lista de tags de cada dia (un
// conjunto), no cuantas descargas recibio cada una
function estructuraMasExpuesta(dias) {
    const cuenta = new Map();
    dias.forEach(d => (d.estructuras_lista || []).forEach(tag => {
        cuenta.set(tag, (cuenta.get(tag) || 0) + 1);
    }));
    if (!cuenta.size) return null;
    let mejor = null;
    cuenta.forEach((n, tag) => { if (!mejor || n > mejor.dias) mejor = { tag, dias: n }; });
    return mejor;
}

function renderResumenMes(datos, min, max, rango) {
    const r = datos.resumen;
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };

    set('calCardEstructuras', num(r.estructuras_alcanzadas));

    if (r.peor_dia) {
        set('calCardPeorDia', fechaCorta(r.peor_dia));
        set('calCardPeorDiaSub', `${num(r.peor_dia_rayos)} descargas`);
    } else {
        set('calCardPeorDia', '—');
        set('calCardPeorDiaSub', 'Sin actividad este mes');
    }

    set('calCardDias', num(r.dias_con_actividad));
    set('calCardDiasSub', `de ${datos.dias_del_mes} días del mes`);

    const expuesta = estructuraMasExpuesta(datos.dias);
    const expEl = document.getElementById('calCardExpuesta');
    if (expuesta) {
        const nombre = nombreEstructura(expuesta.tag);
        expEl.textContent = nombre;
        expEl.title = nombre;             // los tags largos se recortan con ...
        set('calCardExpuestaSub', `${num(expuesta.dias)} ${expuesta.dias === 1 ? 'día con impacto' : 'días con impacto'}`);
    } else {
        expEl.textContent = '—';
        expEl.title = '';
        set('calCardExpuestaSub', 'Sin impactos este mes');
    }
}

// ---- Panel de detalle del dia ----

// Pasa el mismo dia a los dos campos de fecha del sidebar. Usa la instancia
// de flatpickr que cuelga del propio input (_flatpickr) en vez de las
// variables fpInicio/fpFin, que quedan encerradas dentro de initCalendarios()
function fijarRangoFechas(fecha) {
    const ini = document.getElementById('fechaInicio');
    const fin = document.getElementById('fechaFin');
    if (!ini || !fin) return;
    if (ini._flatpickr && fin._flatpickr) {
        ini._flatpickr.setDate(fecha, false);
        fin._flatpickr.setDate(fecha, false);
    } else {
        ini.value = fecha;
        fin.value = fecha;
    }
}

// ---- Mini tarjeta del dia ----
// Se asoma al pasar el cursor por una casilla con datos y queda fija al hacer
// clic. Solo puede haber una fijada: con varias abiertas el calendario deja de
// leerse, que es justo lo que se quiere evitar.
let calFechaFijada = null;

function cerrarTarjetaDia() {
    calFechaFijada = null;
    const t = document.getElementById('calTarjeta');
    if (t) { t.hidden = true; t.classList.remove('fija'); }
    document.querySelectorAll('#calGrilla .cal-dia.seleccionado')
        .forEach(el => el.classList.remove('seleccionado'));
}

// Barras horarias normalizadas contra el propio pico del dia: lo que importa
// aca es la forma de la tormenta ese dia puntual, no compararla con otros
function barrasHorarias(horas, horaPico) {
    const tope = Math.max(1, ...horas);
    return horas.map((n, h) => {
        const alto = n > 0 ? Math.max(Math.round((n / tope) * 100), 6) : 0;
        // La etiqueta va siempre presente (vacia si no toca mostrarla): si el
        // elemento aparece y desaparece, las barras sin numero quedan mas
        // abajo que las que si lo tienen y la fila deja de verse alineada
        const etiqueta = h % 3 === 0 ? String(h).padStart(2, '0') : '';
        return `<div class="cal-hora-barra${h === horaPico ? ' pico' : ''}"
                     title="${String(h).padStart(2, '0')}:00 · ${num(n)} descargas">
                    <span class="cal-hora-relleno" style="height:${alto}%"></span>
                    <span class="cal-hora-etiqueta">${etiqueta}</span>
                </div>`;
    }).join('');
}

// Coloca la tarjeta al lado de la casilla, dentro de .calendario-card. Si no
// cabe a la derecha se voltea a la izquierda. En vertical no basta con encajarla
// en el contenedor: el calendario es mas alto que la ventana, asi que en las
// ultimas semanas del mes la tarjeta quedaba por debajo del pliegue y el mini
// grafico se veia cortado. Por eso el limite final es la ventana visible.
function posicionarTarjetaDia(celda, tarjeta) {
    const cont = document.querySelector('.calendario-card');
    if (!cont) return;
    const rc = cont.getBoundingClientRect();
    const rd = celda.getBoundingClientRect();
    const SEP = 8;
    const w = tarjeta.offsetWidth, h = tarjeta.offsetHeight;

    let left = rd.right - rc.left + SEP;
    if (left + w > cont.clientWidth) left = rd.left - rc.left - w - SEP;
    left = Math.max(0, Math.min(left, cont.clientWidth - w));

    // top va en coordenadas del contenedor; rc.top lo pasa a coordenadas de pantalla
    let top = rd.top - rc.top;
    const desborde = (rc.top + top + h) - (window.innerHeight - SEP);
    if (desborde > 0) top -= desborde;
    if (rc.top + top < SEP) top = SEP - rc.top;

    tarjeta.style.left = `${Math.round(left)}px`;
    tarjeta.style.top = `${Math.round(top)}px`;
}

// fijar=true la deja clavada (con los botones de cerrar y ver en el mapa);
// fijar=false es la vista previa del hover
function mostrarTarjetaDia(fecha, celda, fijar) {
    if (!datosCalendario) return;
    const d = datosCalendario.dias.find(x => x.fecha === fecha);
    const tarjeta = document.getElementById('calTarjeta');
    if (!d || !tarjeta || d.rayos_radio <= 0) return;

    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    const f = new Date(`${fecha}T00:00:00`);
    set('calTjFecha', `${f.getDate()} ${MESES_CORTOS[f.getMonth()]} ${f.getFullYear()}`);
    set('calTjRayos', num(d.rayos_radio));
    set('calTjEstr', num(d.estructuras));
    set('calTjHora', d.hora_pico !== null
        ? `${String(d.hora_pico).padStart(2, '0')}:00–${String((d.hora_pico + 1) % 24).padStart(2, '0')}:00`
        : '—');
    // La polaridad va como signo delante ("+6,4 kA"): escrita entera ("· Positivo")
    // no cabia en la fila y partia la etiqueta en dos lineas
    set('calTjCorriente', `${polaridadSimbolo(d.corriente_max_polaridad)}${num(Math.abs(d.corriente_max), 1)} kA`);
    document.getElementById('calTjHoras').innerHTML = barrasHorarias(d.horas, d.hora_pico);

    tarjeta.dataset.fecha = fecha;
    tarjeta.classList.toggle('fija', !!fijar);
    tarjeta.hidden = false;              // hay que mostrarla antes de medirla
    posicionarTarjetaDia(celda, tarjeta);

    document.querySelectorAll('#calGrilla .cal-dia.seleccionado')
        .forEach(el => el.classList.remove('seleccionado'));
    if (fijar) {
        calFechaFijada = fecha;
        celda.classList.add('seleccionado');
    }
}

function initTarjetaDia() {
    const grilla = document.getElementById('calGrilla');
    const tarjeta = document.getElementById('calTarjeta');
    if (!grilla || !tarjeta) return;

    // Vista previa al pasar por encima. Con una tarjeta fijada el hover no hace
    // nada: si la reemplazara, el dia que estabas estudiando desaparece al
    // mover el raton por el calendario
    grilla.addEventListener('mouseover', ev => {
        if (calFechaFijada) return;
        const dia = ev.target.closest('.cal-dia.activo');
        if (dia && dia.dataset.fecha) mostrarTarjetaDia(dia.dataset.fecha, dia, false);
    });
    grilla.addEventListener('mouseleave', () => {
        if (!calFechaFijada) { tarjeta.hidden = true; tarjeta.classList.remove('fija'); }
    });

    grilla.addEventListener('click', ev => {
        const dia = ev.target.closest('.cal-dia');
        if (!dia) return;
        // Un dia sin datos (o volver a hacer clic en el fijado) cierra la tarjeta
        if (!dia.classList.contains('activo') || calFechaFijada === dia.dataset.fecha) {
            cerrarTarjetaDia();
            return;
        }
        mostrarTarjetaDia(dia.dataset.fecha, dia, true);
    });

    document.getElementById('calTjCerrar').addEventListener('click', cerrarTarjetaDia);
    // El reposicionamiento al cambiar el tamano se hace sobre la casilla fijada,
    // que es la unica que sigue existiendo cuando no hay cursor encima
    window.addEventListener('resize', () => {
        if (!calFechaFijada) return;
        const celda = document.querySelector(`#calGrilla .cal-dia[data-fecha="${calFechaFijada}"]`);
        if (celda) posicionarTarjetaDia(celda, tarjeta);
    });
}
document.addEventListener('DOMContentLoaded', initTarjetaDia);

// ---- Simulador de tormenta ----
// Mapa propio (aparte de currentMap) porque tiene su propio ciclo de vida: la
// capa de animacion de rayos y el ambiente meteorologico llegan en fases
// siguientes y no deben mezclarse con el mapa general
let simMap = null;
let simLayers = { estructuras: null };
let datosSimulador = null;   // { inicio, fin, dias, span, rayos:[{t,lat,lon,c,p}], soleado, ... }

// Motor de reproduccion. simT es el segundo del dia que se esta mostrando
// (0 = 00:00:00, 86400 = fin del dia). La animacion corre sobre un canvas
// aparte para poder pintar miles de destellos sin marcadores de Leaflet.
let simCanvas = null, simCtx = null;         // capa de rayos sobre el mapa
let simDensidad = null, simDensCtx = null;   // histograma de la barra de tiempo
let simParticulas = [];                       // destellos vivos (efimeros, se apagan)
let simImpactos = [];                         // marcas persistentes: donde ya cayo un rayo
let simSeleccion = -1;                        // indice del rayo elegido en la cronologia
let simAnim = null;                           // id de requestAnimationFrame
let simReproduciendo = false;
let simT = 0;                                 // segundo dentro del rango
let simIndiceProx = 0;                        // proximo rayo por lanzar
let simUltimoFrame = 0;                        // timestamp del frame anterior
// El simulador abarca un rango (1 dia a todo el historial). simSpan es su
// duracion total en segundos; simInicio es la fecha de arranque (para el reloj)
let simSpan = 86400;
let simInicio = null;                          // Date del arranque del rango
let simFin = null;                             // Date del ultimo instante del rango
let simDias = 1;                               // cantidad de dias del rango

// Meses abreviados: en una tarjeta de KPI o en la barra de controles no cabe
// "2 de septiembre de 2026", y toLocaleDateString('es-CO') mete el "de"
const MESES_CORTOS = ['ene', 'feb', 'mar', 'abr', 'may', 'jun',
                      'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
const dosDig = (n) => String(n).padStart(2, '0');
const fechaCortaSim = (f) => `${f.getDate()} ${MESES_CORTOS[f.getMonth()]} ${f.getFullYear()}`;
const SIM_VIDA_MS = 1500;                      // cuanto dura visible cada destello
const SIM_MAX_PARTICULAS = 400;               // tope para no ahogar los dias intensos

// Reproduccion adaptativa: a 1x (Normal) el dia entero se recorre en 20 s
// reales, pero el tiempo no avanza parejo: se acelera en las horas vacias y
// entra en camara lenta al llegar a los rayos. simReal es el segundo real de
// reproduccion (que avanza segun el multiplicador de velocidad); un remapeo lo
// convierte al segundo simulado del dia.
const SIM_DURACION_BASE = 20;                  // el dia completo a 1x (Normal) dura 20 s reales
let simVelReproduccion = 1;                    // multiplicador de velocidad (1 Normal, 1.5, 2)
let simReal = 0;                               // segundo real dentro de la reproduccion
let simRemap = { segs: [], realTotal: 0 };     // tramos {simA,simB,realA,realB,activo}
const SIM_LENTO_V = 45;                        // camara lenta ±45 s alrededor de cada rayo
const SIM_BEAT = 1.0;                          // s reales que dura mostrar cada rayo
const SIM_VACIO_MAX = 8;                        // tope de s reales para recorrer todo lo vacio

// Ambiente meteorologico (Fase 2). El bucle de dibujo corre mientras la
// pestana esta a la vista, aunque la reproduccion este en pausa: asi la lluvia
// sigue cayendo si uno pausa en medio de una tormenta.
let simTabActiva = false;
let simNivel = 0;                              // intensidad suavizada 0..1 (sol -> tormenta)
let simFlash = 0;                              // destello de pantalla al caer rayos fuertes
let simLluvia = [];                            // gotas de lluvia
const SIM_VENTANA_CLIMA = 1800;                // 30 min: rayos recientes que definen el clima
const SIM_MAX_GOTAS = 260;

function initSimMap() {
    if (simMap) return;
    let centerLat = 4.4, centerLon = -72.6;
    if (currentData && currentData.estructuras.length) {
        centerLat = currentData.estructuras[0].lat;
        centerLon = currentData.estructuras[0].lon;
    }
    simMap = L.map('simMap').setView([centerLat, centerLon], 13);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: 'Tiles &copy; Esri'
    }).addTo(simMap);
    simLayers.estructuras = L.layerGroup().addTo(simMap);
}

// Dibuja las estructuras filtradas (las mismas que el mapa general, con los
// filtros actuales del sidebar) como base sobre la que caeran los rayos
function dibujarEstructurasSim() {
    if (!simMap || !currentData) return;
    simLayers.estructuras.clearLayers();
    const bounds = L.latLngBounds();
    currentData.estructuras.forEach(est => {
        const latLng = [est.lat, est.lon];
        bounds.extend(latLng);
        const marcador = est.es_portico
            ? L.marker(latLng, { icon: getPorticoIcon() })
            : L.circleMarker(latLng, {
                radius: 5, fillColor: colorProteccion(est), color: '#fff',
                weight: 1.2, opacity: 1, fillOpacity: 0.9
            });
        ligarInfo(marcador, est);
        marcador.addTo(simLayers.estructuras);
    });
    if (currentData.estructuras.length) simMap.fitBounds(bounds, { padding: [40, 40] });
}

function renderSimulador(datos) {
    datosSimulador = datos;

    // Alcance del rango: define la duracion total y el arranque para el reloj
    simSpan = datos.span || 86400;
    simDias = datos.dias || 1;
    simInicio = datos.inicio ? new Date(`${datos.inicio}T00:00:00`) : null;
    simFin = datos.fin ? new Date(`${datos.fin}T23:59:00`) : null;

    // Junto con el pico de corriente se guarda CUANDO cayo: la descarga mas
    // intensa no tiene por que estar en el dia critico ni en la hora critica,
    // asi que la tarjeta lleva su propia fecha debajo
    const rayos = datos.rayos || [];
    let corrMax = 0, tCorrMax = 0;
    rayos.forEach(r => { if (Math.abs(r.c) > Math.abs(corrMax)) { corrMax = r.c; tCorrMax = r.t; } });

    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('simKpiRadio', num(datos.total_radio));
    // Sin separador de miles: es el mismo numero que se escribio en el filtro
    set('simKpiRadioValor', datos.radio);
    set('simKpiRegion', num(datos.total_region));
    set('simKpiCorriente', rayos.length ? `${num(Math.abs(corrMax), 1)} kA` : '—');
    set('simKpiCorrienteSub', rayos.length && simInicio
        ? fechaCortaSim(new Date(simInicio.getTime() + tCorrMax * 1000))
        : '');

    actualizarKpisPico(rayos);
    actualizarPeriodoSim();

    // Cada rango nuevo arranca la reproduccion desde cero
    pausarSim();
    simRemap = construirRemapSim(rayos, SIM_DURACION_BASE);
    simT = 0;
    simReal = 0;
    simIndiceProx = 0;
    simParticulas = [];
    simImpactos = [];
    simSeleccion = -1;
    simUltimoFrame = 0;
    simNivel = 0;
    simFlash = 0;

    const hayRayos = datos.rayos && datos.rayos.length > 0;
    const btnPlay = document.getElementById('simPlay');
    const btnRe = document.getElementById('simReiniciar');
    if (btnPlay) btnPlay.disabled = !hayRayos;
    if (btnRe) btnRe.disabled = !hayRayos;

    simFilaResaltada = -1;
    renderTablaSimulador(datos);

    dimensionarSimCanvas();
    limpiarSimCanvas();
    dibujarTimeline();
    actualizarReloj();
}

// ---- Tabla cronologica de descargas ----

// Hora de la tabla: HH:MM:SS en un dia, DD/MM HH:MM:SS en rangos. Los segundos
// vienen en el dato de origen y son los que distinguen descargas de la misma
// rafaga, que es justo lo que se quiere leer en la cronologia
function horaCortaTabla(t) {
    const p = n => String(n).padStart(2, '0');
    if (simDias <= 1 || !simInicio) {
        const s = t % 86400;
        return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
    }
    const f = new Date(simInicio.getTime() + t * 1000);
    return `${p(f.getDate())}/${p(f.getMonth() + 1)} ${p(f.getHours())}:${p(f.getMinutes())}:${p(f.getSeconds())}`;
}

// Los porticos llegan con llave compuesta "circuito␟TAG" porque su tag se
// repite en varias locaciones. Mostrar solo la ultima parte daba "PORT", que no
// identifica nada: lo que distingue a un portico es el circuito, asi que van
// los dos. Una estructura normal llega sin ␟ y se muestra tal cual.
function nombreEstructura(e) {
    if (!e) return '—';
    const partes = e.split('␟');
    return partes.length > 1 ? `${partes[0]} · ${partes[1]}` : partes[0];
}

function polaridadSimbolo(p) {
    const t = (p || '').toLowerCase();
    if (t.startsWith('pos')) return '+';
    if (t.startsWith('neg')) return '−';
    return '';
}

function renderTablaSimulador(datos) {
    const cont = document.getElementById('simTablaCuerpo');
    if (!cont) return;
    const rayos = datos.rayos || [];
    if (!rayos.length) {
        cont.innerHTML = '<p class="sim-tabla-vacia">Sin descargas dentro del radio en este período.</p>';
        return;
    }
    const filas = rayos.map((r, i) => {
        const pol = polaridadSimbolo(r.p);
        const est = escapar(nombreEstructura(r.e));
        return `<tr data-i="${i}" data-t="${r.t}">
            <td class="st-hora">${horaCortaTabla(r.t)}</td>
            <td class="st-num">${num(Math.abs(r.c), 1)}</td>
            <td class="st-pol ${pol === '+' ? 'pos' : 'neg'}">${pol}</td>
            <td class="st-est" title="${est}">${est}</td>
            <td class="st-num">${num(r.d, 0)}</td>
        </tr>`;
    }).join('');
    cont.innerHTML = `<table class="sim-tabla">
        <thead><tr><th>Hora</th><th>kA</th><th>±</th><th>Estructura</th><th>m</th></tr></thead>
        <tbody>${filas}</tbody></table>`;
}

// Resalta en la tabla el ultimo rayo ya caido y lo trae a la vista. Solo toca
// el DOM cuando la fila cambia, asi es barato aunque corra en cada frame
let simFilaResaltada = -1;
function resaltarFilaSim(idxForzado) {
    const cont = document.getElementById('simTablaCuerpo');
    if (!cont) return;
    // Por defecto el ultimo rayo ya caido; al hacer clic se fuerza esa fila
    const idx = idxForzado !== undefined ? idxForzado : simIndiceProx - 1;
    if (idx === simFilaResaltada) return;
    simFilaResaltada = idx;
    const prev = cont.querySelector('tr.activa');
    if (prev) prev.classList.remove('activa');
    if (idx < 0) return;
    const fila = cont.querySelector(`tr[data-i="${idx}"]`);
    if (fila) {
        fila.classList.add('activa');
        fila.scrollIntoView({ block: 'nearest' });
    }
}

// Clic en una fila: saltar a ese instante, pausar, resaltar la fila y marcar
// el rayo en el mapa (naranja sol), centrandolo para no tener que buscarlo
document.addEventListener('click', ev => {
    const fila = ev.target.closest('#simTablaCuerpo tr[data-t]');
    if (!fila) return;
    const idx = Number(fila.dataset.i);
    pausarSim();
    buscarSimT(Number(fila.dataset.t));
    resaltarFilaSim(idx);
    simSeleccion = idx;
    const r = (datosSimulador ? datosSimulador.rayos : [])[idx];
    if (r && simMap) simMap.panTo([r.lat, r.lon], { animate: true, duration: 0.5 });
});

// ---- Simulador: motor de animacion ----

function initSimCanvas() {
    if (!simCanvas) {
        simCanvas = document.getElementById('simCanvas');
        simDensidad = document.getElementById('simDensidad');
        if (!simCanvas || !simDensidad) return;
        simCtx = simCanvas.getContext('2d');
        simDensCtx = simDensidad.getContext('2d');

        // La barra de tiempo se pinta bajo el cursor sobre el propio canvas
        const tl = document.getElementById('simTimeline');
        let arrastrando = false, reanudar = false;
        const tiempoDesdeEvento = (e) => {
            const rect = tl.getBoundingClientRect();
            const x = Math.max(0, Math.min(rect.width, (e.touches ? e.touches[0].clientX : e.clientX) - rect.left));
            return (x / rect.width) * simSpan;
        };
        tl.addEventListener('pointerdown', (e) => {
            arrastrando = true; reanudar = simReproduciendo;
            pausarSim();
            buscarSimT(tiempoDesdeEvento(e));
            tl.setPointerCapture(e.pointerId);
        });
        tl.addEventListener('pointermove', (e) => { if (arrastrando) buscarSimT(tiempoDesdeEvento(e)); });
        tl.addEventListener('pointerup', () => { arrastrando = false; if (reanudar) reproducirSim(); });

        if (simMap) {
            simMap.on('resize', () => { dimensionarSimCanvas(); dibujarTimeline(); });
            // Al mover o hacer zoom con la animacion en pausa, los destellos
            // quedarian anclados a pixeles viejos: se limpian
            simMap.on('move zoom', () => { if (!simReproduciendo) limpiarSimCanvas(); });
        }
        window.addEventListener('resize', () => {
            // dimensionarSimCanvas ya recalcula el alto del bloque (mapa +
            // tabla); Leaflet necesita ademas su propio invalidateSize para
            // redibujar las teselas al nuevo tamaño
            dimensionarSimCanvas();
            if (simMap) simMap.invalidateSize();
            dibujarTimeline();
        });
    }
    dimensionarSimCanvas();
}

function dimensionarSimCanvas() {
    // Primero se fija el alto del bloque (mapa + tabla): el mapa recien
    // despues de eso queda con su tamaño final, que es lo que hay que leer
    // para dimensionar el canvas que va encima
    ajustarAlturaSimCuerpo();

    const cont = document.getElementById('simMap');
    if (!cont || !simCanvas || !simDensidad) return;
    const dpr = window.devicePixelRatio || 1;

    const w = cont.clientWidth, h = cont.clientHeight;
    simCanvas.width = Math.round(w * dpr); simCanvas.height = Math.round(h * dpr);
    simCanvas.style.width = w + 'px'; simCanvas.style.height = h + 'px';
    simCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const tl = document.getElementById('simTimeline');
    const dw = tl.clientWidth, dh = tl.clientHeight;
    simDensidad.width = Math.round(dw * dpr); simDensidad.height = Math.round(dh * dpr);
    simDensidad.style.width = dw + 'px'; simDensidad.style.height = dh + 'px';
    simDensCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

// El mapa y la tabla no tienen una altura propia (el mapa solo pide un minimo,
// la tabla crece con sus filas), asi que sin esto el bloque se queda chico y
// deja aire muerto abajo, o la tabla se estira sin limite y empuja la pagina.
// Se le fija al conjunto la altura que sobra hasta el fondo de la ventana, y
// ahi adentro el mapa y la tabla se reparten ese alto (align-items: stretch);
// la tabla se desborda hacia su propio scroll, no hacia la pagina.
function ajustarAlturaSimCuerpo() {
    const cuerpo = document.querySelector('.sim-cuerpo');
    // El padding que hay que descontar es el del panel de vidrio que envuelve al
    // mapa, no el de la vista (que ya no tiene: solo apila tarjetas y panel)
    const seccion = document.getElementById('simPanel');
    if (!cuerpo || !seccion) return;
    // En pantallas angostas pasa a columna (mapa arriba, tabla abajo con un
    // tope fijo en CSS): ahi la altura no se fuerza, cada uno usa la suya
    if (window.innerWidth <= 900) { cuerpo.style.height = ''; return; }
    const top = cuerpo.getBoundingClientRect().top;
    // Aire despues del bloque: lo que cierra el panel de vidrio (su padding
    // inferior MAS su borde de 1px, que tambien ocupa) y el margen del layout
    // general, para terminar justo al ras sin cortar
    const cs = getComputedStyle(seccion);
    const cierrePanel = (parseFloat(cs.paddingBottom) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
    const margenLayout = 24; // 1.5rem, el mismo que .dashboard-layout deja arriba
    // floor y no el valor crudo: el top viene con decimales y al propagarse por
    // la cadena de contenedores el navegador redondea hacia arriba, dejando la
    // pagina 1px mas alta que la ventana y sacando una barra de desplazamiento
    const alto = Math.floor(window.innerHeight - top - cierrePanel - margenLayout);
    // El piso se calcula contra la pantalla, no fijo: en una laptop baja un
    // minimo de 360px forzaria scroll; en un monitor grande queda holgado
    const piso = Math.min(360, Math.round(window.innerHeight * 0.35));
    cuerpo.style.height = Math.max(piso, alto) + 'px';
}

function limpiarSimCanvas() {
    if (simCtx && simCanvas) simCtx.clearRect(0, 0, simCanvas.width, simCanvas.height);
}

// El reloj se adapta al rango: hora del dia en un solo dia, fecha + hora cuando
// abarca varios dias
function formatearHora(seg) {
    seg = Math.max(0, Math.min(simSpan - 1, Math.floor(seg)));
    const p = (n) => String(n).padStart(2, '0');
    if (simDias <= 1 || !simInicio) {
        const s = seg % 86400;
        return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
    }
    // Mismo formato de fecha que el bloque de periodo de al lado: con el "de"
    // que mete toLocaleDateString ('25 de ago') las dos lecturas chocaban.
    // Los segundos van siempre: el dato de origen los trae y sin ellos dos
    // descargas del mismo minuto parecian simultaneas
    const f = new Date(simInicio.getTime() + seg * 1000);
    return `${f.getDate()} ${MESES_CORTOS[f.getMonth()]} ${p(f.getHours())}:${p(f.getMinutes())}:${p(f.getSeconds())}`;
}

function actualizarReloj() {
    const reloj = document.getElementById('simReloj');
    if (reloj) reloj.textContent = formatearHora(simT);
}

// Periodo completo que abarca la simulacion, para no tener que deducirlo del
// reloj (que solo marca el instante en curso)
function actualizarPeriodoSim() {
    const ini = document.getElementById('simPeriodoIni');
    const fin = document.getElementById('simPeriodoFin');
    if (ini) ini.textContent = simInicio ? `${fechaCortaSim(simInicio)} · 00:00` : '—';
    if (fin) fin.textContent = simFin ? `${fechaCortaSim(simFin)} · 23:59` : '—';
}

// Dos KPIs independientes, y esa independencia es el punto:
//   Dia Pico  = el dia del rango con mas descargas.
//   Hora Pico = la franja horaria concreta con mas descargas de TODO el rango.
// No se busca la hora pico dentro del dia pico: un dia puede acumular muchas
// descargas repartidas a lo largo de la jornada mientras otro las concentra en
// una sola hora, y esa concentracion es la que interesa. Por eso la hora pico
// lleva su propia fecha debajo: puede caer en un dia distinto al dia pico.
// Ambos se calculan sobre los rayos dentro del radio (los que se animan), igual
// que la corriente maxima.
function actualizarKpisPico(rayos) {
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    if (!rayos.length || !simInicio) {
        set('simKpiDia', '—');
        set('simKpiHora', '—');
        set('simKpiHoraSub', '');
        return;
    }

    const porDia = new Array(Math.max(1, simDias)).fill(0);
    const porHora = new Array(Math.max(1, Math.ceil(simSpan / 3600))).fill(0);
    rayos.forEach(r => {
        porDia[Math.min(porDia.length - 1, Math.floor(r.t / 86400))]++;
        porHora[Math.min(porHora.length - 1, Math.floor(r.t / 3600))]++;
    });
    // Recorrido a mano en vez de Math.max(...arr): un rango de anios son decenas
    // de miles de franjas y el spread revienta la pila
    let iDia = 0, iHora = 0;
    for (let i = 1; i < porDia.length; i++) if (porDia[i] > porDia[iDia]) iDia = i;
    for (let i = 1; i < porHora.length; i++) if (porHora[i] > porHora[iHora]) iHora = i;

    set('simKpiDia', fechaCortaSim(new Date(simInicio.getTime() + iDia * 86400000)));

    const fHora = new Date(simInicio.getTime() + iHora * 3600000);
    const h = fHora.getHours();
    set('simKpiHora', `${dosDig(h)}:00 - ${dosDig((h + 1) % 24)}:00`);
    set('simKpiHoraSub', fechaCortaSim(fHora));
}

// Marcas de la barra de tiempo, adaptadas al alcance del rango: horas en un
// dia, dias o meses en rangos mas largos
function ticksTimeline() {
    const DIA = 86400;
    if (simDias <= 1) {
        const ts = [];
        for (let h = 0; h <= 24; h += 3) ts.push({ sim: h * 3600, txt: h === 24 ? '24' : String(h).padStart(2, '0') });
        return ts;
    }
    let paso;
    if (simDias <= 10) paso = 1;
    else if (simDias <= 31) paso = 5;
    else if (simDias <= 92) paso = 15;
    else paso = 30;
    const soloMes = simDias > 92;
    const ts = [];
    for (let d = 0; d <= simDias; d += paso) {
        const f = simInicio ? new Date(simInicio.getTime() + d * DIA * 1000) : null;
        const txt = f ? f.toLocaleDateString('es-CO', soloMes ? { month: 'short' } : { day: 'numeric', month: 'short' }) : String(d);
        ts.push({ sim: d * DIA, txt });
    }
    return ts;
}

// Barra de tiempo: histograma de actividad de fondo, cuadricula adaptativa y el
// cabezal de reproduccion en el instante actual
function dibujarTimeline() {
    if (!simDensCtx || !simDensidad) return;
    const W = simDensidad.clientWidth || simDensidad.width;
    const H = simDensidad.clientHeight || simDensidad.height;
    simDensCtx.clearRect(0, 0, W, H);

    // Fondo
    simDensCtx.fillStyle = 'rgba(255,255,255,0.04)';
    simDensCtx.fillRect(0, 0, W, H);

    const rayos = datosSimulador ? datosSimulador.rayos : [];
    // Histograma: una barra por columna de pixel, altura por cantidad de rayos
    if (rayos && rayos.length) {
        const bins = new Array(W).fill(0);
        rayos.forEach(r => { bins[Math.min(W - 1, Math.floor(r.t / simSpan * W))]++; });
        const maxBin = Math.max(1, ...bins);
        simDensCtx.fillStyle = 'rgba(248,113,113,0.55)';
        for (let x = 0; x < W; x++) {
            if (!bins[x]) continue;
            const alto = Math.max(2, (bins[x] / maxBin) * (H - 14));
            simDensCtx.fillRect(x, H - alto, 1, alto);
        }
    }

    // Cuadricula adaptativa (horas, dias o meses segun el rango)
    simDensCtx.font = '9px Inter, sans-serif';
    simDensCtx.textAlign = 'center';
    ticksTimeline().forEach(tk => {
        const x = (tk.sim / simSpan) * W;
        simDensCtx.strokeStyle = 'rgba(255,255,255,0.12)';
        simDensCtx.beginPath();
        simDensCtx.moveTo(x + 0.5, 0); simDensCtx.lineTo(x + 0.5, H - 12);
        simDensCtx.stroke();
        simDensCtx.fillStyle = 'rgba(255,255,255,0.45)';
        simDensCtx.fillText(tk.txt, Math.max(14, Math.min(W - 14, x)), H - 2);
    });

    // Region transcurrida
    const px = (simT / simSpan) * W;
    simDensCtx.fillStyle = 'rgba(201,0,22,0.12)';
    simDensCtx.fillRect(0, 0, px, H - 12);

    // Cabezal
    simDensCtx.strokeStyle = 'rgb(201,0,22)';
    simDensCtx.lineWidth = 2;
    simDensCtx.beginPath();
    simDensCtx.moveTo(px, 0); simDensCtx.lineTo(px, H - 10);
    simDensCtx.stroke();
    simDensCtx.fillStyle = 'rgb(201,0,22)';
    simDensCtx.beginPath();
    simDensCtx.arc(px, 4, 3.5, 0, 2 * Math.PI);
    simDensCtx.fill();
}

function simLatLngAPixel(lat, lon) {
    return simMap.latLngToContainerPoint([lat, lon]);
}

// Los tres estados de un rayo en el simulador se leen por color. Antes el color
// salia de la escala de corriente, igual para el que caia y el que ya habia
// caido: solo cambiaba la animacion y a simple vista eran lo mismo. La
// intensidad se sigue leyendo en la columna kA de la cronologia.
// (Esto es SOLO del simulador; el mapa de calor conserva su escala de corriente)
const SIM_COL_CAYENDO = '#ff5f1f';    // naranja: esta cayendo ahora mismo
const SIM_COL_CAIDO   = '#ffd21e';    // amarillo: ya cayo, queda registrado
const SIM_COL_SELECC  = '#ffa41b';    // naranja sol: el elegido en la cronologia

// Marca persistente de un rayo. Guarda su indice para poder reconocer cual es
// el seleccionado en la cronologia sin volver a buscar por coordenadas
function marcaImpacto(r, i) {
    return { lat: r.lat, lon: r.lon, i };
}

function lanzarParticula(r, i) {
    const m = marcaImpacto(r, i);
    // Destello efimero (el "flash" del impacto, se apaga en SIM_VIDA_MS)
    if (simParticulas.length >= SIM_MAX_PARTICULAS) simParticulas.shift();
    simParticulas.push({ lat: m.lat, lon: m.lon, nacio: performance.now() });
    // Marca persistente: queda en el mapa el resto del dia
    simImpactos.push(m);
    // Relampago de pantalla: ocasional y solo cuando ya hay tormenta, para que
    // no sea un estrobo constante
    if (simNivel > 0.4 && Math.random() < 0.22) simFlash = 0.9;
}

// Marcas de los rayos que ya cayeron: nucleo + aura + la onda (el anillo del
// impacto) conservada, todo respirando en fase, para que se lea que ahi cayo
// un rayo aunque el destello ya se haya apagado
function dibujarImpactos(ahora) {
    if (!simCtx || !simMap || !simImpactos.length) return;
    const fase = ahora * 0.003;                          // respiracion compartida
    const pulso = 0.2 + 0.08 * Math.sin(fase);           // aura mas intensa
    const anilloR = 13 + 2 * Math.sin(fase);             // la onda respira
    const anilloA = 0.35 + 0.13 * Math.sin(fase + 1);    // intensidad de la onda
    simCtx.save();
    simImpactos.forEach(m => {
        // El seleccionado se pinta aparte y por encima, para que no quede
        // tapado por las marcas de los rayos que cayeron despues
        if (m.i === simSeleccion) return;
        const pt = simLatLngAPixel(m.lat, m.lon);
        // Aura (mas grande y notoria)
        simCtx.globalAlpha = pulso;
        simCtx.fillStyle = SIM_COL_CAIDO;
        simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 14, 0, 2 * Math.PI); simCtx.fill();
        // Onda: el anillo del impacto, conservado y respirando
        simCtx.globalAlpha = Math.max(0.15, anilloA);
        simCtx.strokeStyle = SIM_COL_CAIDO;
        simCtx.lineWidth = 1.8;
        simCtx.beginPath(); simCtx.arc(pt.x, pt.y, anilloR, 0, 2 * Math.PI); simCtx.stroke();
        // Nucleo
        simCtx.globalAlpha = 0.95;
        simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 3.5, 0, 2 * Math.PI); simCtx.fill();
        // Centro blanco
        simCtx.globalAlpha = 0.9;
        simCtx.fillStyle = '#fff';
        simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 1.5, 0, 2 * Math.PI); simCtx.fill();
    });
    simCtx.restore();
}

// El rayo elegido en la cronologia. Se dibuja al final (por encima de todo) y
// mucho mas grande que una marca normal: no basta con el color, porque queda
// entre decenas de marcas amarillas y el naranja del destello dura 1,5 s
function dibujarSeleccion(ahora) {
    if (!simCtx || !simMap || simSeleccion < 0) return;
    const rayos = datosSimulador ? datosSimulador.rayos : [];
    const r = rayos[simSeleccion];
    if (!r) return;
    const pt = simLatLngAPixel(r.lat, r.lon);
    const fase = ahora * 0.005;
    const lat1 = 1 + 0.15 * Math.sin(fase);              // latido del anillo interno
    const expand = (ahora % 1600) / 1600;                // onda que sale y se apaga

    simCtx.save();
    // Halo
    const halo = simCtx.createRadialGradient(pt.x, pt.y, 0, pt.x, pt.y, 30);
    halo.addColorStop(0, 'rgba(255,164,27,0.55)');
    halo.addColorStop(1, 'rgba(255,164,27,0)');
    simCtx.fillStyle = halo;
    simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 30, 0, 2 * Math.PI); simCtx.fill();
    // Onda que se expande y se desvanece, para que el ojo lo encuentre solo
    simCtx.globalAlpha = 0.7 * (1 - expand);
    simCtx.strokeStyle = SIM_COL_SELECC;
    simCtx.lineWidth = 2.5;
    simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 10 + expand * 26, 0, 2 * Math.PI); simCtx.stroke();
    // Anillo fijo que late
    simCtx.globalAlpha = 0.95;
    simCtx.lineWidth = 2.5;
    simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 11 * lat1, 0, 2 * Math.PI); simCtx.stroke();
    // Nucleo naranja con centro blanco
    simCtx.globalAlpha = 1;
    simCtx.fillStyle = SIM_COL_SELECC;
    simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 5.5, 0, 2 * Math.PI); simCtx.fill();
    simCtx.fillStyle = '#fff';
    simCtx.beginPath(); simCtx.arc(pt.x, pt.y, 2.2, 0, 2 * Math.PI); simCtx.fill();
    simCtx.restore();
}

// Solo dibuja los destellos de rayo; el lienzo lo limpia dibujarFrameSim
function dibujarDestellos(ahora) {
    if (!simCtx || !simMap) return;
    simParticulas = simParticulas.filter(p => ahora - p.nacio < SIM_VIDA_MS);
    simParticulas.forEach(p => {
        const edad = (ahora - p.nacio) / SIM_VIDA_MS;   // 0..1
        const pt = simLatLngAPixel(p.lat, p.lon);
        const alpha = 1 - edad;

        simCtx.save();
        // Anillo que se expande. Va mas grande y grueso que la onda de las
        // marcas ya caidas: el rayo que esta cayendo tiene que ganar la mirada,
        // y con el radio anterior quedaba mas chico que las marcas amarillas
        simCtx.globalAlpha = alpha * 0.6;
        simCtx.strokeStyle = SIM_COL_CAYENDO;
        simCtx.lineWidth = 2.6;
        simCtx.beginPath();
        simCtx.arc(pt.x, pt.y, 5 + edad * 28, 0, 2 * Math.PI);
        simCtx.stroke();

        // Nucleo brillante
        simCtx.globalAlpha = alpha;
        const grad = simCtx.createRadialGradient(pt.x, pt.y, 0, pt.x, pt.y, 15);
        grad.addColorStop(0, '#ffffff');
        grad.addColorStop(0.35, SIM_COL_CAYENDO);
        grad.addColorStop(1, 'rgba(0,0,0,0)');
        simCtx.fillStyle = grad;
        simCtx.beginPath();
        simCtx.arc(pt.x, pt.y, 15, 0, 2 * Math.PI);
        simCtx.fill();
        simCtx.restore();
    });
}

// ---- Ambiente meteorologico ----
// El clima sale del ritmo de rayos recientes: cuantos cayeron en la ultima
// media hora simulada. 0 = despejado, pocos = lluvia, muchos = tormenta.
function intensidadEnVentana() {
    const rayos = datosSimulador ? datosSimulador.rayos : [];
    if (!rayos.length) return 0;
    let count = 0;
    for (let i = simIndiceProx - 1; i >= 0; i--) {
        if (rayos[i].t <= simT - SIM_VENTANA_CLIMA) break;
        count++;
    }
    return count;
}

function nivelObjetivo() {
    return Math.min(1, intensidadEnVentana() / 6);
}

// Suaviza el nivel hacia su objetivo (constante de tiempo ~0.4s) para que el
// clima no salte de golpe entre despejado y tormenta
function actualizarNivel(dt) {
    const k = 1 - Math.exp(-dt / 0.4);
    simNivel += (nivelObjetivo() - simNivel) * k;
    if (simNivel < 0.003) simNivel = 0;
}

// Oscurecimiento del cielo segun la intensidad; se queda translucido para no
// tapar el mapa
function dibujarCielo(W, H) {
    if (simNivel <= 0.001) return;
    simCtx.save();
    simCtx.globalAlpha = simNivel * 0.4;
    const g = simCtx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, 'rgb(12,15,28)');
    g.addColorStop(1, 'rgb(30,36,52)');
    simCtx.fillStyle = g;
    simCtx.fillRect(0, 0, W, H);
    simCtx.restore();
}

const SIM_NUBES = [
    { x: 0.15, y: 0.05, r: 0.30, v: 0.006 },
    { x: 0.50, y: 0.02, r: 0.36, v: 0.004 },
    { x: 0.80, y: 0.07, r: 0.32, v: 0.008 },
    { x: 0.35, y: 0.09, r: 0.26, v: 0.005 }
];
function dibujarNubes(W, H, ahora) {
    if (simNivel <= 0.05) return;
    simCtx.save();
    simCtx.globalAlpha = simNivel * 0.6;
    SIM_NUBES.forEach(n => {
        const R = n.r * Math.min(W, 520);
        const periodo = W + 2 * R;
        const cx = (((n.x * W + ahora * n.v * 6) % periodo) + periodo) % periodo - R;
        const cy = n.y * H + 12;
        const g = simCtx.createRadialGradient(cx, cy, 0, cx, cy, R);
        g.addColorStop(0, 'rgba(8,10,20,0.9)');
        g.addColorStop(1, 'rgba(8,10,20,0)');
        simCtx.fillStyle = g;
        simCtx.beginPath();
        simCtx.arc(cx, cy, R, 0, 2 * Math.PI);
        simCtx.fill();
    });
    simCtx.restore();
}

function actualizarLluvia(dt, W, H) {
    while (simLluvia.length < SIM_MAX_GOTAS) {
        simLluvia.push({ x: Math.random(), y: Math.random() * H, len: 8 + Math.random() * 12, vel: 380 + Math.random() * 320 });
    }
    const n = Math.floor(simNivel * SIM_MAX_GOTAS);
    for (let i = 0; i < n; i++) {
        const g = simLluvia[i];
        g.y += g.vel * dt;
        if (g.y > H + g.len) { g.y = -g.len; g.x = Math.random(); }
    }
}
function dibujarLluvia(W, H) {
    const n = Math.floor(simNivel * SIM_MAX_GOTAS);
    if (n <= 0) return;
    simCtx.save();
    simCtx.strokeStyle = `rgba(200,215,240,${0.15 + simNivel * 0.3})`;
    simCtx.lineWidth = 1.1;
    simCtx.beginPath();
    for (let i = 0; i < n; i++) {
        const g = simLluvia[i];
        const x = g.x * W;
        simCtx.moveTo(x, g.y);
        simCtx.lineTo(x - 2.5, g.y + g.len);   // leve diagonal
    }
    simCtx.stroke();
    simCtx.restore();
}

// El sol solo asoma con el cielo despejado y se apaga al llegar la lluvia
function dibujarSol(W, H, ahora) {
    const op = Math.max(0, (0.2 - simNivel) / 0.2);
    if (op <= 0) return;
    const cx = W - 54, cy = 50, R = 20;
    simCtx.save();
    simCtx.globalAlpha = op;
    const halo = simCtx.createRadialGradient(cx, cy, 0, cx, cy, R * 2.6);
    halo.addColorStop(0, 'rgba(255,214,120,0.55)');
    halo.addColorStop(1, 'rgba(255,214,120,0)');
    simCtx.fillStyle = halo;
    simCtx.beginPath(); simCtx.arc(cx, cy, R * 2.6, 0, 2 * Math.PI); simCtx.fill();

    simCtx.strokeStyle = 'rgba(255,206,90,0.9)';
    simCtx.lineWidth = 2;
    for (let k = 0; k < 8; k++) {
        const a = (k / 8) * Math.PI * 2 + ahora * 0.0003;
        simCtx.beginPath();
        simCtx.moveTo(cx + Math.cos(a) * (R + 6), cy + Math.sin(a) * (R + 6));
        simCtx.lineTo(cx + Math.cos(a) * (R + 14), cy + Math.sin(a) * (R + 14));
        simCtx.stroke();
    }
    const disco = simCtx.createRadialGradient(cx, cy, 0, cx, cy, R);
    disco.addColorStop(0, '#fff3c4');
    disco.addColorStop(1, '#ffca3a');
    simCtx.fillStyle = disco;
    simCtx.beginPath(); simCtx.arc(cx, cy, R, 0, 2 * Math.PI); simCtx.fill();
    simCtx.restore();
}

function actualizarBadgeClima() {
    const el = document.getElementById('simClima');
    if (!el) return;
    let txt, clase;
    if (simNivel < 0.1) { txt = '☀️ Despejado'; clase = 'despejado'; }
    else if (simNivel < 0.45) { txt = '🌧️ Lluvia'; clase = 'lluvia'; }
    else { txt = '⛈️ Tormenta'; clase = 'tormenta'; }
    if (el.dataset.clima !== clase) {
        el.textContent = txt;
        el.dataset.clima = clase;
        el.className = 'sim-clima ' + clase;
    }
}

// Un frame completo: limpia y apila cielo, nubes, lluvia, destellos, flash y sol
function dibujarFrameSim(dt, ahora) {
    if (!simCtx || !simCanvas) return;
    const W = simCanvas.clientWidth, H = simCanvas.clientHeight;
    simCtx.save();
    simCtx.setTransform(1, 0, 0, 1, 0, 0);
    simCtx.clearRect(0, 0, simCanvas.width, simCanvas.height);
    simCtx.restore();

    dibujarCielo(W, H);
    dibujarNubes(W, H, ahora);
    actualizarLluvia(dt, W, H);
    dibujarLluvia(W, H);
    dibujarImpactos(ahora);
    dibujarDestellos(ahora);
    dibujarSeleccion(ahora);

    // Destello de pantalla de los relampagos, que decae rapido
    simFlash = Math.max(0, simFlash - dt * 5);
    if (simFlash > 0) {
        simCtx.save();
        simCtx.globalAlpha = Math.min(0.18, simFlash * 0.18);
        simCtx.fillStyle = '#fff';
        simCtx.fillRect(0, 0, W, H);
        simCtx.restore();
    }

    dibujarSol(W, H, ahora);
}

// ---- Reproduccion adaptativa ----
// Reparte el tiempo real del dia: las horas vacias se recorren rapido (un
// presupuesto chico y fijo) y cada rayo recibe un "beat" de camara lenta. Si
// hay demasiados rayos para el objetivo (30/60 s), los beats se comprimen para
// no pasarse. Devuelve tramos que mapean segundo real <-> segundo simulado.
function construirRemapSim(rayos, objetivo) {
    // Ventanas de camara lenta: ±V alrededor de cada rayo, fusionando las que
    // se solapan y contando cuantos rayos cae en cada una
    const ventanas = [];
    rayos.forEach(r => {
        const a = Math.max(0, r.t - SIM_LENTO_V), b = Math.min(simSpan, r.t + SIM_LENTO_V);
        const ult = ventanas[ventanas.length - 1];
        if (ult && a <= ult.b) { ult.b = Math.max(ult.b, b); ult.n++; }
        else ventanas.push({ a, b, n: 1 });
    });

    // Tramos alternando vacio / activo cubriendo todo el dia
    const segs = [];
    let cur = 0;
    ventanas.forEach(w => {
        if (w.a > cur) segs.push({ simA: cur, simB: w.a, activo: false });
        segs.push({ simA: w.a, simB: w.b, activo: true, n: w.n });
        cur = w.b;
    });
    if (cur < simSpan) segs.push({ simA: cur, simB: simSpan, activo: false });

    const simVacio = segs.reduce((s, x) => s + (x.activo ? 0 : x.simB - x.simA), 0);
    const activoTotal = segs.reduce((s, x) => s + (x.activo ? x.n * SIM_BEAT : 0), 0);
    const realVacio = simVacio ? Math.min(SIM_VACIO_MAX, objetivo * 0.2) : 0;
    // Si sobran rayos para el objetivo, se comprimen los beats
    const disponible = Math.max(0, objetivo - realVacio);
    const escala = activoTotal > disponible ? disponible / activoTotal : 1;

    let real = 0;
    segs.forEach(s => {
        const simDur = s.simB - s.simA;
        const realDur = s.activo
            ? s.n * SIM_BEAT * escala
            : (simVacio ? realVacio * (simDur / simVacio) : 0);
        s.realA = real; s.realB = real + realDur; real += realDur;
    });
    return { segs, realTotal: real };
}

function remapRealASim(real) {
    if (real <= 0 || !simRemap.segs.length) return 0;
    if (real >= simRemap.realTotal) return simSpan;
    for (const s of simRemap.segs) {
        if (real <= s.realB) {
            const f = s.realB > s.realA ? (real - s.realA) / (s.realB - s.realA) : 1;
            return s.simA + f * (s.simB - s.simA);
        }
    }
    return simSpan;
}

function remapSimAReal(sim) {
    if (sim <= 0 || !simRemap.segs.length) return 0;
    if (sim >= simSpan) return simRemap.realTotal;
    for (const s of simRemap.segs) {
        if (sim <= s.simB) {
            const f = s.simB > s.simA ? (sim - s.simA) / (s.simB - s.simA) : 1;
            return s.realA + f * (s.realB - s.realA);
        }
    }
    return simRemap.realTotal;
}

// ---- Bucle de reproduccion ----
// Corre mientras la pestana esta a la vista, aunque este en pausa: asi el
// ambiente (lluvia, nubes, sol) sigue vivo. La reproduccion solo avanza el
// reloj cuando simReproduciendo es true. Se usa setTimeout en vez de rAF para
// que corra tambien cuando el compositor no entrega frames por su cuenta.
function simBucle() {
    if (!simTabActiva) return;
    const ahora = performance.now();
    let dt = simUltimoFrame ? (ahora - simUltimoFrame) / 1000 : 0;
    simUltimoFrame = ahora;
    if (dt > 0.5) dt = 0.5;                    // la pestana estuvo en segundo plano

    if (simReproduciendo) {
        const rayos = datosSimulador ? datosSimulador.rayos : [];
        // El tiempo real avanza segun el multiplicador de velocidad; el remapeo
        // lo convierte al tiempo del dia, rapido en lo vacio y lento en los rayos
        simReal += dt * simVelReproduccion;
        if (simReal >= simRemap.realTotal) simReal = simRemap.realTotal;
        simT = remapRealASim(simReal);

        while (simIndiceProx < rayos.length && rayos[simIndiceProx].t <= simT) {
            lanzarParticula(rayos[simIndiceProx]);
            simIndiceProx++;
        }
        if (simReal >= simRemap.realTotal) { simT = simSpan; if (simParticulas.length === 0) pausarSim(); }
    }

    actualizarNivel(dt);
    dibujarFrameSim(dt, ahora);
    dibujarTimeline();
    actualizarReloj();
    actualizarBadgeClima();
    // Solo durante la reproduccion: en pausa el resaltado lo fija el clic o el
    // salto en la barra, y el bucle no debe pisarlo
    if (simReproduciendo) resaltarFilaSim();

    simAnim = setTimeout(simBucle, 16);
}

function iniciarBucleSim() {
    if (simTabActiva) return;
    simTabActiva = true;
    simUltimoFrame = 0;
    simBucle();
}

function detenerBucleSim() {
    simTabActiva = false;
    if (simAnim) { clearTimeout(simAnim); simAnim = null; }
}

function reproducirSim() {
    if (simReproduciendo || !datosSimulador || !datosSimulador.rayos.length) return;
    // Si estaba al final, reinicia desde el comienzo del dia
    if (simReal >= simRemap.realTotal) {
        simReal = 0; simT = 0; simIndiceProx = 0; simParticulas = []; simImpactos = [];
    }
    // Al arrancar de nuevo la marca del rayo elegido deja de tener sentido
    simSeleccion = -1;
    simReproduciendo = true;
    const btn = document.getElementById('simPlay');
    if (btn) { btn.classList.add('reproduciendo'); btn.textContent = '❚❚'; btn.title = 'Pausar'; }
}

function pausarSim() {
    simReproduciendo = false;
    const btn = document.getElementById('simPlay');
    if (btn) { btn.classList.remove('reproduciendo'); btn.textContent = '▶'; btn.title = 'Reproducir'; }
}

function alternarSimPlay() {
    if (simReproduciendo) pausarSim(); else reproducirSim();
}

function buscarSimT(seg) {
    // Cualquier salto invalida la seleccion previa (el clic en la cronologia la
    // vuelve a fijar despues de llamar aca)
    simSeleccion = -1;
    simT = Math.max(0, Math.min(simSpan, seg));
    const rayos = datosSimulador ? datosSimulador.rayos : [];
    // Estrictamente mayor: un rayo justo en el instante buscado se considera ya
    // caido (queda marcado y resaltado en la tabla)
    simIndiceProx = rayos.findIndex(r => r.t > simT);
    if (simIndiceProx < 0) simIndiceProx = rayos.length;
    simParticulas = [];
    // Las marcas persistentes reflejan el estado hasta este instante: se
    // reconstruyen con los rayos anteriores (al saltar atras desaparecen las de
    // despues; al saltar adelante aparecen las intermedias)
    simImpactos = rayos.slice(0, simIndiceProx).map(marcaImpacto);
    // El tiempo real de reproduccion se alinea con el instante buscado
    simReal = remapSimAReal(simT);
    // El clima salta al del nuevo instante en vez de arrastrar el anterior
    simNivel = nivelObjetivo();
    dibujarTimeline();
    actualizarReloj();
    resaltarFilaSim();
}

function reiniciarSim() {
    pausarSim();
    buscarSimT(0);
}

// ---- Vista de datos: distribucion de corriente ----

let graficaCorriente = null;

// Percentil por interpolacion lineal sobre la muestra ya ordenada, el mismo
// criterio que usa numpy por defecto
function percentil(ordenados, p) {
    if (ordenados.length === 0) return 0;
    if (ordenados.length === 1) return ordenados[0];
    const pos = (ordenados.length - 1) * p;
    const bajo = Math.floor(pos), alto = Math.ceil(pos);
    if (bajo === alto) return ordenados[bajo];
    return ordenados[bajo] + (pos - bajo) * (ordenados[alto] - ordenados[bajo]);
}

// Regla de Freedman-Diaconis: el ancho de banda sale del rango intercuartil,
// asi que no colapsa con la cola larga de corrientes altas. Con muestras muy
// chicas cae a la raiz cuadrada del tamano.
function numeroDeBins(ordenados) {
    const n = ordenados.length;
    if (n < 30) return Math.max(1, Math.ceil(Math.sqrt(n)));
    const iqr = percentil(ordenados, 0.75) - percentil(ordenados, 0.25);
    const rango = ordenados[n - 1] - ordenados[0];
    if (iqr <= 0 || rango <= 0) return Math.ceil(Math.sqrt(n));
    const ancho = 2 * iqr / Math.cbrt(n);
    return Math.min(60, Math.max(5, Math.ceil(rango / ancho)));
}

function estadisticosCorriente(rayos) {
    // Magnitud, no valor con signo: un rayo de -150 kA es de los mas intensos
    const vals = rayos.map(magnitudCorriente).filter(v => v > 0).sort((a, b) => a - b);
    if (vals.length === 0) return null;
    const media = vals.reduce((a, b) => a + b, 0) / vals.length;
    const varianza = vals.reduce((a, b) => a + (b - media) ** 2, 0) / vals.length;
    return {
        vals,
        n: vals.length,
        media,
        desvio: Math.sqrt(varianza),
        mediana: percentil(vals, 0.5),
        p99: percentil(vals, 0.99),
        min: vals[0],
        max: vals[vals.length - 1]
    };
}

const kA = v => `${v.toLocaleString('es-CO', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} kA`;

function renderGraficaCorriente(data) {
    const lienzo = document.getElementById('graficaCorriente');
    const nota = document.getElementById('graficaNota');
    if (!lienzo || typeof Chart === 'undefined') return;

    const est = estadisticosCorriente(data.rayos || []);

    document.getElementById('medidaMedia').textContent = est ? kA(est.media) : '—';
    document.getElementById('medidaMediana').textContent = est ? kA(est.mediana) : '—';
    document.getElementById('medidaP99').textContent = est ? kA(est.p99) : '—';

    if (graficaCorriente) { graficaCorriente.destroy(); graficaCorriente = null; }

    if (!est) {
        if (nota) nota.textContent = 'No hay descargas dentro del radio con los filtros actuales.';
        const ctx = lienzo.getContext('2d');
        ctx.clearRect(0, 0, lienzo.width, lienzo.height);
        return;
    }

    if (nota) {
        nota.textContent = `${est.n.toLocaleString('es-CO')} descargas · magnitud entre ${kA(est.min)} y ${kA(est.max)}`;
    }

    // Histograma normalizado a densidad: el area suma 1, que es lo que permite
    // superponer la campana teorica en la misma escala
    const bins = numeroDeBins(est.vals);
    const ancho = (est.max - est.min) / bins || 1;
    const cuentas = new Array(bins).fill(0);
    est.vals.forEach(v => {
        const i = Math.min(bins - 1, Math.floor((v - est.min) / ancho));
        cuentas[i]++;
    });

    const centros = cuentas.map((_, i) => est.min + ancho * (i + 0.5));
    const densidades = cuentas.map(c => c / (est.n * ancho));

    // Campana normal con la media y el desvio de la muestra. Se dibuja aunque
    // no ajuste: justamente muestra cuanto se aparta la realidad de una normal,
    // que en corrientes de rayo es mucho (la cola derecha es larguisima).
    const gauss = centros.map(x => {
        const z = (x - est.media) / est.desvio;
        return Math.exp(-0.5 * z * z) / (est.desvio * Math.sqrt(2 * Math.PI));
    });

    const topeY = Math.max(...densidades, ...gauss) * 1.12;
    // Chart.js ordena los datasets por "order" ascendente y despues los dibuja
    // recorriendo esa lista AL REVES. O sea que el order mas ALTO se pinta
    // primero y queda al fondo. Por eso las barras llevan el numero mayor: asi
    // las lineas de referencia y la campana quedan por encima y no se pierden.
    const linea = (x, color, etiqueta, guion) => ({
        label: etiqueta,
        type: 'line',
        data: [{ x, y: 0 }, { x, y: topeY }],
        borderColor: color,
        borderWidth: 2,
        borderDash: guion,
        pointRadius: 0,
        fill: false,
        order: 0
    });

    graficaCorriente = new Chart(lienzo, {
        data: {
            datasets: [
                {
                    label: 'Distribución',
                    type: 'bar',
                    data: centros.map((x, i) => ({ x, y: densidades[i] })),
                    backgroundColor: 'rgba(37, 99, 235, 0.85)',
                    borderWidth: 0,
                    barPercentage: 1,
                    categoryPercentage: 1,
                    order: 2
                },
                {
                    label: 'Campana gaussiana',
                    type: 'line',
                    data: centros.map((x, i) => ({ x, y: gauss[i] })),
                    borderColor: '#7dd3fc',
                    borderWidth: 2,
                    pointRadius: 0,
                    tension: 0.35,
                    fill: false,
                    order: 1
                },
                linea(est.media, '#f59e0b', 'Media', [8, 5]),
                linea(est.mediana, '#22c55e', 'Mediana', [8, 5]),
                linea(est.p99, '#ef4444', 'Percentil 99', [2, 3])
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'nearest', intersect: false },
            scales: {
                x: {
                    type: 'linear',
                    title: { display: true, text: 'Corriente (kA)', color: '#a3a3a3' },
                    ticks: { color: '#a3a3a3' },
                    grid: { color: 'rgba(255,255,255,0.06)' }
                },
                y: {
                    title: { display: true, text: 'Densidad', color: '#a3a3a3' },
                    ticks: { color: '#a3a3a3' },
                    grid: { color: 'rgba(255,255,255,0.06)' },
                    beginAtZero: true
                }
            },
            plugins: {
                // La leyenda se arma en HTML al costado: asi cada muestra imita
                // el trazo real (linea, guion, punteado) en vez de una caja de
                // color, y los valores pueden ir debajo en vez de en la etiqueta
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: items => `${items[0].parsed.x.toFixed(1)} kA`,
                        label: item => `${item.dataset.label}: ${item.parsed.y.toFixed(4)}`
                    }
                }
            }
        }
    });
}

// ---- Vista de datos: tablas ----

const num = (v, dec = 0) => (v ?? 0).toLocaleString('es-CO', { minimumFractionDigits: dec, maximumFractionDigits: dec });
const siNo = v => v ? 'Sí' : 'No';

// Agrupa las estructuras por la clave que se le pase y arma los totales.
// Los resumenes por circuito y por campo son la misma cuenta a distinta altura.
function agrupar(estructuras, clave, extras) {
    const mapa = new Map();
    estructuras.forEach(e => {
        const k = clave(e);
        if (!mapa.has(k)) {
            mapa.set(k, Object.assign({ estructuras: 0, afectadas: 0, impactos: 0, corriente_max: 0 }, extras(e)));
        }
        const g = mapa.get(k);
        g.estructuras++;
        if ((e.impactos || 0) > 0) g.afectadas++;
        g.impactos += e.impactos || 0;
        // Magnitud: las corrientes vienen con signo y la mayoria son negativas
        if (Math.abs(e.corriente_max || 0) > Math.abs(g.corriente_max)) g.corriente_max = e.corriente_max;
    });
    return [...mapa.values()];
}

// Cada tabla declara sus columnas y de donde salen sus filas. `tipo` decide el
// formato de la celda; `num: true` hace que se ordene y alinee como numero.
// `soloConImpactos` deja fuera lo que no recibio ninguna descarga: en estas
// tablas una fila en cero no aporta nada y solo diluye lo que si importa.
const TABLAS = {
    estructuras: {
        columnas: [
            { clave: 'id', etiqueta: 'TAG' },
            { clave: 'campo', etiqueta: 'Campo' },
            { clave: 'locacion', etiqueta: 'Locación' },
            { clave: 'portico', etiqueta: 'Pórtico / Circuito' },
            { clave: 'impactos', etiqueta: 'Impactos', num: true, tipo: 'criticidad' },
            { clave: 'corriente_max', etiqueta: 'Corriente máx (kA)', num: true, dec: 1 },
            { clave: 'dist_min', etiqueta: 'Dist. mínima (m)', num: true },
            { clave: 'error_min', etiqueta: '± error (m)', num: true, tipo: 'error' },
            { clave: 'dps', etiqueta: 'DPS', tipo: 'proteccion' },
            { clave: 'dsd', etiqueta: 'DSD', tipo: 'proteccion' },
            { clave: 'lat', etiqueta: 'Latitud', num: true, dec: 6 },
            { clave: 'lon', etiqueta: 'Longitud', num: true, dec: 6 }
        ],
        filas: d => d.estructuras,
        soloConImpactos: true,
        ordenDefecto: { clave: 'impactos', dir: 'desc' },
        nota: 'solo estructuras impactadas'
    },
    rayos: {
        columnas: [
            { clave: 'fecha', etiqueta: 'Fecha' },
            { clave: 'corriente', etiqueta: 'Corriente (kA)', num: true, dec: 1 },
            { clave: 'magnitud', etiqueta: 'Magnitud (kA)', num: true, dec: 1 },
            { clave: 'polaridad', etiqueta: 'Polaridad' },
            { clave: 'lat', etiqueta: 'Latitud', num: true, dec: 4 },
            { clave: 'lon', etiqueta: 'Longitud', num: true, dec: 4 }
        ],
        filas: d => d.rayos.map(r => Object.assign({ magnitud: magnitudCorriente(r) }, r)),
        // Las fechas vienen como AAAA-MM-DD, asi que ordenarlas como texto de
        // mayor a menor ya deja arriba la descarga mas reciente
        ordenDefecto: { clave: 'fecha', dir: 'desc' }
    },
    circuito: {
        columnas: [
            { clave: 'portico', etiqueta: 'Pórtico / Circuito' },
            { clave: 'campo', etiqueta: 'Campo' },
            { clave: 'locacion', etiqueta: 'Locación' },
            { clave: 'estructuras', etiqueta: 'Estructuras', num: true },
            { clave: 'afectadas', etiqueta: 'Afectadas', num: true },
            { clave: 'impactos', etiqueta: 'Impactos', num: true, tipo: 'criticidad' },
            { clave: 'corriente_max', etiqueta: 'Corriente máx (kA)', num: true, dec: 1 }
        ],
        filas: d => agrupar(d.estructuras, e => e.portico,
            e => ({ portico: e.portico || '(sin asignar)', campo: e.campo || '—', locacion: e.locacion || '—' })),
        soloConImpactos: true,
        ordenDefecto: { clave: 'impactos', dir: 'desc' },
        nota: 'solo circuitos impactados'
    },
    campo: {
        columnas: [
            { clave: 'campo', etiqueta: 'Campo' },
            { clave: 'locacion', etiqueta: 'Locación' },
            { clave: 'estructuras', etiqueta: 'Estructuras', num: true },
            { clave: 'afectadas', etiqueta: 'Afectadas', num: true },
            { clave: 'impactos', etiqueta: 'Impactos', num: true, tipo: 'criticidad' },
            { clave: 'corriente_max', etiqueta: 'Corriente máx (kA)', num: true, dec: 1 }
        ],
        filas: d => agrupar(d.estructuras, e => `${e.campo}||${e.locacion}`,
            e => ({ campo: e.campo || '(sin asignar)', locacion: e.locacion || '—' })),
        soloConImpactos: true,
        ordenDefecto: { clave: 'impactos', dir: 'desc' },
        nota: 'solo con impactos'
    }
};

let tablaActual = 'estructuras';
// Orden y busqueda son por tabla: cambiar de pestana no deberia perder como
// tenias ordenada la anterior
const ordenPorTabla = {};
const busquedaPorTabla = {};

function celda(col, fila, escala) {
    const v = fila[col.clave];

    // Un dato que no existe se marca como tal en vez de mostrarse como cero
    if (col.num && (v === null || v === undefined)) return '<span class="celda-num">—</span>';

    // El error de localizacion del rayo mas cercano se atenua y se marca cuando
    // supera a la distancia: ahi el "estuvo a 20 m" deja de ser afirmable
    if (col.tipo === 'error') {
        const excede = (fila.dist_min ?? 0) < v;
        return `<span class="celda-num ${excede ? 'error-domina' : 'error-ok'}" ` +
               `title="${excede ? 'El error de posición del rayo supera la distancia medida' : 'Error menor que la distancia medida'}">± ${num(v)}</span>`;
    }

    if (col.tipo === 'proteccion') {
        return v
            ? '<span class="pill-prot">Sí</span>'
            : `<span class="pill-no">${siNo(v)}</span>`;
    }

    if (col.tipo === 'criticidad') {
        const n = v || 0;
        if (n === 0) return '<span class="celda-num">0</span>';
        const t = escala.rango ? (n - escala.min) / escala.rango : 1;
        return `<span class="celda-num"><span class="punto-crit" style="background:${colorCriticidad(t)}"></span>${num(n)}</span>`;
    }

    if (col.num) return `<span class="celda-num">${num(v, col.dec || 0)}</span>`;
    return escapar(v);
}

function renderTabla(data) {
    const cabeza = document.getElementById('tablaCabeza');
    const cuerpo = document.getElementById('tablaCuerpo');
    const conteo = document.getElementById('tablaConteo');
    if (!cabeza || !cuerpo || !data) return;

    const def = TABLAS[tablaActual];
    let filas = def.filas(data);
    if (def.soloConImpactos) filas = filas.filter(f => (f.impactos || 0) > 0);
    const total = filas.length;

    // Busqueda: texto plano contra todas las columnas de la fila
    const q = (busquedaPorTabla[tablaActual] || '').trim().toLowerCase();
    if (q) {
        filas = filas.filter(f => def.columnas.some(c => String(f[c.clave] ?? '').toLowerCase().includes(q)));
    }

    // Si el usuario no toco ningun encabezado manda el orden por defecto de la
    // tabla, que es el que responde su pregunta natural: las mas golpeadas
    // primero, y en los rayos la descarga mas reciente arriba
    const orden = ordenPorTabla[tablaActual] || def.ordenDefecto;
    if (orden) {
        const col = def.columnas.find(c => c.clave === orden.clave);
        const signo = orden.dir === 'asc' ? 1 : -1;
        filas = filas.slice().sort((a, b) => {
            const va = a[orden.clave], vb = b[orden.clave];
            if (col && col.num) return signo * ((va || 0) - (vb || 0));
            return signo * String(va ?? '').localeCompare(String(vb ?? ''), 'es', { numeric: true });
        });
    }

    // Escala de criticidad relativa a lo que se esta viendo, igual que el mapa
    const impactos = filas.map(f => f.impactos || 0).filter(n => n > 0);
    const escala = { min: impactos.length ? Math.min(...impactos) : 0, max: impactos.length ? Math.max(...impactos) : 0 };
    escala.rango = escala.max - escala.min;

    cabeza.innerHTML = '<tr>' + def.columnas.map(c => {
        const act = orden && orden.clave === c.clave;
        const flecha = act ? (orden.dir === 'asc' ? ' ▲' : ' ▼') : '';
        return `<th class="${c.num ? 'col-num' : ''}${act ? ' ordenada' : ''}" data-clave="${c.clave}" title="Ordenar por ${escapar(c.etiqueta)}">${escapar(c.etiqueta)}${flecha}</th>`;
    }).join('') + '</tr>';

    // Concatenar el HTML y asignarlo de una es bastante mas rapido que crear
    // cada celda con createElement cuando hay miles de filas
    if (filas.length === 0) {
        const motivo = q
            ? 'Ninguna fila coincide con la búsqueda.'
            : 'Ninguna recibió impactos con los filtros actuales.';
        cuerpo.innerHTML = `<tr><td class="tabla-vacia" colspan="${def.columnas.length}">${motivo}</td></tr>`;
    } else {
        // Concatenar el HTML y asignarlo de una es bastante mas rapido que crear
        // cada celda con createElement cuando hay miles de filas
        cuerpo.innerHTML = filas.map(f =>
            '<tr>' + def.columnas.map(c => `<td class="${c.num ? 'col-num' : ''}">${celda(c, f, escala)}</td>`).join('') + '</tr>'
        ).join('');
    }

    if (conteo) {
        const base = q && filas.length !== total
            ? `${num(filas.length)} de ${num(total)} filas`
            : `${num(total)} ${total === 1 ? 'fila' : 'filas'}`;
        conteo.textContent = def.nota ? `${base} · ${def.nota}` : base;
    }
}

// ---- Panel de detalle de la estructura seleccionada ----
// El mapa dice donde esta y cuanto le pego; el panel dice como es. Los datos
// vienen ya agrupados y sin campos vacios desde el backend.
function ocultarDetalle() {
    const panel = document.getElementById('detallePanel');
    if (panel) panel.hidden = true;
}

function mostrarDetalle(detalle) {
    const panel = document.getElementById('detallePanel');
    const cuerpo = document.getElementById('detalleCuerpo');
    const titulo = document.getElementById('detalleTag');
    if (!panel || !cuerpo || !titulo) return;

    if (!detalle || !detalle.grupos || detalle.grupos.length === 0) {
        ocultarDetalle();
        return;
    }

    titulo.textContent = detalle.tag;
    cuerpo.innerHTML = '';

    if (detalle.duplicado) {
        const aviso = document.createElement('p');
        aviso.className = 'buscador-nota aviso';
        aviso.textContent = 'Este tag aparece más de una vez en el inventario; se muestra la primera fila.';
        cuerpo.appendChild(aviso);
    }

    detalle.grupos.forEach(g => {
        const h = document.createElement('h4');
        h.textContent = g.grupo;
        cuerpo.appendChild(h);

        const dl = document.createElement('dl');
        g.campos.forEach(c => {
            const dt = document.createElement('dt');
            dt.textContent = c.etiqueta;
            const dd = document.createElement('dd');
            dd.textContent = c.valor;
            dl.appendChild(dt);
            dl.appendChild(dd);
        });
        cuerpo.appendChild(dl);
    });

    cuerpo.scrollTop = 0;
    panel.hidden = false;
}

document.addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'detalleCerrar') ocultarDetalle();
});

function renderDashboard(data) {
    // KPIs
    document.getElementById('kpiTotalEstructuras').textContent = data.kpis.total_estructuras.toLocaleString('es-CO');
    document.getElementById('kpiAfectadas').textContent = data.kpis.estructuras_afectadas.toLocaleString('es-CO');
    document.getElementById('kpiTotalRayos').textContent = data.kpis.total_rayos.toLocaleString('es-CO');
    document.getElementById('kpiRadio').textContent = data.kpis.radio;

    const enRango = data.kpis.total_rayos_rango || 0;
    document.getElementById('kpiRayosRango').textContent = enRango.toLocaleString('es-CO');

    // Tasa de exposicion: cuanto del total del rango llego a caer dentro del
    // radio de alguna estructura. es-CO da coma decimal y punto de miles
    const tasa = document.getElementById('kpiTasaExposicion');
    if (enRango > 0) {
        const pct = (100 * data.kpis.total_rayos) / enRango;
        // Por debajo de 0,01% el redondeo mostraria "0,00 %", que se leeria
        // como que no cayo ninguno
        const txt = pct > 0 && pct < 0.01
            ? '<0,01'
            : pct.toLocaleString('es-CO', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        tasa.textContent = `${txt} %`;
    } else {
        tasa.textContent = '—';
    }

    if (data.detalle_estructura) {
        mostrarDetalle(data.detalle_estructura);
    } else {
        ocultarDetalle();
    }

    // Render Map
    renderMap(data, { ajustarVista: true });

    // La vista de datos solo se repinta si esta a la vista: sobre un contenedor
    // oculto Chart.js mide 0 px y sale una grafica vacia
    if (vistaActiva() === 'datos') renderVistaDatos(data);
    // El calendario tiene su propio mes, pero comparte los filtros de ubicacion
    if (vistaActiva() === 'calendario' && typeof recargarCalendario === 'function') recargarCalendario();
    // El simulador comparte estructuras y filtros: se redibujan y se recarga el dia
    if (vistaActiva() === 'simulador') {
        dibujarEstructurasSim();
        if (typeof recargarSimulador === 'function') recargarSimulador();
    }
}

// Escala del calendario: un solo tono (el rojo de marca de GeoPark,
// --accent-primary) que va de pastel a intenso segun la cantidad de rayos.
// Va aparte de ESCALA_CALOR porque esa es multicolor y se usa en el mapa de
// calor, la tabla y el top de estructuras; el calendario no debe tocarlas.
const ESCALA_CALENDARIO = [
    { t: 0.00, rgb: [254, 226, 226] }, // rojo pastel
    { t: 0.50, rgb: [248, 113, 113] },
    { t: 1.00, rgb: [201, 0, 22] }     // --accent-primary
];

function colorCalendario(t) {
    return interpolarEscala(ESCALA_CALENDARIO, t);
}

// Paradas del gradiente de criticidad. Las mismas que usa la barra de la
// leyenda en styles.css: si se cambian aca, cambiarlas alla tambien.
const ESCALA_CALOR = [
    { t: 0.00, rgb: [29, 78, 216] },
    { t: 0.35, rgb: [6, 182, 212] },
    { t: 0.60, rgb: [250, 204, 21] },
    { t: 0.80, rgb: [249, 115, 22] },
    { t: 1.00, rgb: [220, 38, 38] }
];

// Color de cada estructura en el Mapa General (en el de calor el color
// significa criticidad). Son dos categorias porque separar DPS de DSD en el
// mapa no aportaria: las 7 estructuras con DSD tienen tambien DPS, asi que la
// distincion se hace con los subfiltros, no con el color.
const COLOR_PROTECCION = {
    ninguna: '#2563eb',  // azul
    protegida: '#9333ea' // purpura
};

function colorProteccion(est) {
    return (est.dps || est.dsd) ? COLOR_PROTECCION.protegida : COLOR_PROTECCION.ninguna;
}

// Paradas del gradiente de corriente de las descargas. Amarillo los rayos mas
// suaves, rojo los mas intensos. Va aparte de ESCALA_CALOR porque mide otra
// cosa: aquella gradua estructuras por impactos, esta rayos por kilo-amperios.
const ESCALA_CORRIENTE = [
    { t: 0.00, rgb: [253, 224, 71] },
    { t: 0.50, rgb: [249, 115, 22] },
    { t: 1.00, rgb: [220, 38, 38] }
];

// t va de 0 a 1 e interpola entre las paradas contiguas
function interpolarEscala(escala, t) {
    t = Math.max(0, Math.min(1, t));
    for (let i = 1; i < escala.length; i++) {
        const a = escala[i - 1], b = escala[i];
        if (t <= b.t) {
            const f = (t - a.t) / (b.t - a.t);
            const c = a.rgb.map((v, j) => Math.round(v + f * (b.rgb[j] - v)));
            return `rgb(${c[0]},${c[1]},${c[2]})`;
        }
    }
    return `rgb(${escala[escala.length - 1].rgb.join(',')})`;
}

function colorCriticidad(t) {
    return interpolarEscala(ESCALA_CALOR, t);
}

// La corriente viene con signo y el 62 % de las descargas son negativas, asi
// que lo que grada el color es la magnitud: un rayo de -150 kA es de los mas
// peligrosos, no de los mas suaves.
function magnitudCorriente(rayo) {
    return Math.abs(Number(rayo.corriente) || 0);
}

// La escala se recalcula sobre los rayos que quedaron tras los filtros: el rojo
// marca siempre el mas fuerte de lo que se esta viendo, no un tope absoluto.
// La barra queda fija en pantalla aunque no haya rayos (marca 0), igual que la
// de criticidad: que aparezca y desaparezca hace saltar el resto de la leyenda.
function renderLeyendaCorriente(min, max) {
    const caja = document.getElementById('leyendaCorriente');
    if (!caja) return;

    const fmt = v => v.toLocaleString('es-CO', { maximumFractionDigits: 1 });
    document.getElementById('corrienteMin').textContent = fmt(min);
    document.getElementById('corrienteMax').textContent = fmt(max);
}

// La escala es relativa a lo que se esta viendo: el rojo marca siempre la peor
// estructura del recorte actual. Sin la leyenda el color seria ambiguo, porque
// el mismo tono significa cosas distintas segun el filtro.
function renderLeyendaCalor(min, max) {
    const caja = document.getElementById('heatLegend');
    if (!caja) return;

    document.getElementById('heatMin').textContent = min.toLocaleString('es-CO');
    document.getElementById('heatMax').textContent = max.toLocaleString('es-CO');
    caja.style.display = 'flex';
}

// Un rayo se dibuja igual en toda la app: mismo simbolo en la linea de tiempo
// que al destacar las descargas de una estructura
function getBoltIcon(color, tam = 20) {
    return L.divIcon({
        html: `<svg width="${tam}" height="${tam}" viewBox="0 0 24 24" style="overflow:visible;"><polygon points="13,2 4,14 12,14 11,22 20,10 12,10" fill="${color}" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
        className: 'custom-bolt-icon',
        iconSize: [tam, tam],
        iconAnchor: [tam / 2, tam * 0.55]
    });
}

// Color propio de los porticos de derivacion. Elegido entre los que quedaban
// libres: el azul y el morado son de los postes, el amarillo-naranja-rojo de
// los rayos y la criticidad, y el cian de los circulos de radio.
const COLOR_PORTICO = '#84cc16';

// Los porticos se dibujan como rombo y no como circulo, para distinguirlos de
// un vistazo. El borde va mas grueso que el de los postes (2.5 contra 1.5), que
// es lo que los separa incluso cuando quedan encima de un poste.
function getPorticoIcon(tam = 16) {
    const m = tam / 2;
    return L.divIcon({
        html: `<svg width="${tam}" height="${tam}" viewBox="0 0 24 24" style="overflow:visible;">
                 <polygon points="12,1 23,12 12,23 1,12" fill="${COLOR_PORTICO}"
                          stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/>
               </svg>`,
        className: 'icono-portico',
        iconSize: [tam, tam],
        iconAnchor: [m, m]
    });
}

// Mismo contenido para el globo al pasar el cursor y para el que queda fijo al
// hacer clic: la informacion de una estructura es una sola
function infoEstructura(est) {
    // En un portico la columna de circuito guarda en realidad su locacion, asi
    // que rotularla como "Circuito" seria decir algo falso
    if (est.es_portico) {
        return `
            <b>Pórtico:</b> ${est.tag || est.id}<br>
            <b>Locación:</b> ${est.locacion || '—'}<br>
            <b>Campo:</b> ${est.campo || '—'}<br>
            <b>Impactos:</b> ${(est.impactos || 0).toLocaleString('es-CO')}
        `;
    }
    return `
        <b>TAG:</b> ${est.tag || est.id}<br>
        <b>Circuito:</b> ${est.detalles.Circuito}<br>
        <b>Impactos:</b> ${(est.impactos || 0).toLocaleString('es-CO')}<br>
        <b>DSD:</b> ${est.detalles.DSD}<br>
        <b>DPS:</b> ${est.detalles.DPS}
    `;
}

// sticky hace que el globo siga al cursor: con estructuras tan juntas, uno
// anclado al centro del punto suele quedar tapando a la vecina
function ligarInfo(marcador, est) {
    const html = infoEstructura(est);
    marcador.bindPopup(html);
    marcador.bindTooltip(html, { sticky: true, direction: 'top', offset: [0, -6], className: 'tooltip-estructura' });
}

function umbralActual() {
    const slider = document.getElementById('umbralImpactos');
    return slider ? parseInt(slider.value, 10) || 0 : 0;
}

// El slider se reajusta al rango del recorte visible. Si el valor anterior
// quedo fuera del nuevo rango se recorta, para no dejar el mapa vacio tras
// cambiar un filtro
function configurarUmbral(min, max) {
    const slider = document.getElementById('umbralImpactos');
    if (!slider) return;
    slider.min = min;
    slider.max = max;
    if (parseInt(slider.value, 10) > max || parseInt(slider.value, 10) < min) slider.value = min;
    pintarValorUmbral(min);
}

function pintarValorUmbral(min) {
    const slider = document.getElementById('umbralImpactos');
    const salida = document.getElementById('umbralValor');
    if (!slider || !salida) return;
    const v = parseInt(slider.value, 10);
    salida.textContent = v <= min ? 'todas' : `≥ ${v.toLocaleString('es-CO')}`;
}

function ocultarTopPanel() {
    const p = document.getElementById('topPanel');
    if (p) p.style.display = 'none';
}

// El mapa dice donde mirar; la lista da el nombre para ir a inspeccionar
function renderTopPanel(data, min, max) {
    const panel = document.getElementById('topPanel');
    const lista = document.getElementById('topLista');
    if (!panel || !lista) return;

    const rango = max - min || 1;
    const umbral = umbralActual();
    const top = data.estructuras
        .filter(e => (e.impactos || 0) > 0 && (e.impactos || 0) >= umbral)
        .sort((a, b) => b.impactos - a.impactos)
        .slice(0, 10);

    if (top.length === 0) {
        panel.style.display = 'none';
        return;
    }

    lista.innerHTML = '';
    top.forEach(est => {
        const t = (est.impactos - min) / rango;
        const li = document.createElement('li');
        li.innerHTML = `
            <span class="top-dot" style="background:${colorCriticidad(t)}"></span>
            <span class="top-tag">${est.id}</span>
            <span class="top-n">${est.impactos.toLocaleString('es-CO')}</span>
        `;
        li.title = `${est.detalles.Circuito} · ${est.impactos} impactos`;
        li.addEventListener('click', () => {
            currentMap.setView([est.lat, est.lon], 16, { animate: true });
            destacarEstructura(est, data);
        });
        lista.appendChild(li);
    });
    panel.style.display = 'flex';
}

// Distancia haversine en metros, la misma metrica que usa el BallTree del
// backend, para que lo resaltado coincida con lo que se conto
function distanciaMetros(lat1, lon1, lat2, lon2) {
    const R = 6371000;
    const rad = Math.PI / 180;
    const dLat = (lat2 - lat1) * rad;
    const dLon = (lon2 - lon1) * rad;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
}

// Muestra el radio de la estructura y las descargas concretas que cayeron
// dentro: convierte el "16 impactos" en algo verificable
function destacarEstructura(est, data, { limpiar = true } = {}) {
    if (limpiar && layers.destacado) currentMap.removeLayer(layers.destacado);
    if (limpiar || !layers.destacado) layers.destacado = L.layerGroup();

    const radio = data.kpis.radio;
    L.circle([est.lat, est.lon], {
        radius: radio,
        color: '#fff',
        weight: 2,
        dashArray: '5,5',
        fill: false
    }).addTo(layers.destacado);

    data.rayos
        .filter(r => distanciaMetros(est.lat, est.lon, r.lat, r.lon) <= radio)
        .forEach(r => {
            L.marker([r.lat, r.lon], { icon: getBoltIcon('#fde047') })
                .bindPopup(`<b>Corriente:</b> ${r.corriente} kA<br><b>Fecha:</b> ${r.fecha}`)
                .addTo(layers.destacado);
        });

    layers.destacado.addTo(currentMap);
}

// ajustarVista solo va en true cuando llegan datos nuevos: al mover el umbral o
// cambiar de modo, reencuadrar tiraria abajo el zoom que hizo el usuario
function renderMap(data, { ajustarVista = false } = {}) {
    if (!currentMap) {
        // Init Map
        let centerLat = data.estructuras.length > 0 ? data.estructuras[0].lat : 4.4;
        let centerLon = data.estructuras.length > 0 ? data.estructuras[0].lon : -72.6;
        
        currentMap = L.map('map').setView([centerLat, centerLon], 13);
        
        // Esri World Imagery
        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            attribution: 'Tiles &copy; Esri'
        }).addTo(currentMap);
    }

    // Clean old layers
    if (layers.structures) currentMap.removeLayer(layers.structures);
    if (layers.radii) currentMap.removeLayer(layers.radii);
    if (layers.strikes) currentMap.removeLayer(layers.strikes);
    if (layers.destacado) { currentMap.removeLayer(layers.destacado); layers.destacado = null; }
    if (layers.calorFondo) { currentMap.removeLayer(layers.calorFondo); layers.calorFondo = null; }

    layers.structures = L.layerGroup();
    layers.radii = L.layerGroup();
    layers.strikes = L.layerGroup();

    const mode = document.querySelector('input[name="mapMode"]:checked').value;
    // En modo calor los marcadores y los circulos de radio taparian el
    // gradiente, que es justamente lo que se quiere leer. La excepcion es
    // cuando ninguna estructura recibio impactos: ahi no hay gradiente que
    // tapar y el mapa quedaria en blanco, asi que se pintan como en el general.
    const sinNingunImpacto = !data.estructuras.some(e => (e.impactos || 0) > 0);
    const mostrarEstructuras = mode !== 'heatmap' || sinNingunImpacto;

    // Draw Structures
    let bounds = L.latLngBounds();
    data.estructuras.forEach(est => {
        let latLng = [est.lat, est.lon];
        bounds.extend(latLng);

        if (!mostrarEstructuras) return;

        // Los porticos de derivacion llevan rombo verde; los postes, circulo
        const marcador = est.es_portico
            ? L.marker(latLng, { icon: getPorticoIcon() })
            : L.circleMarker(latLng, {
                radius: 6,
                fillColor: colorProteccion(est),
                color: '#fff',
                weight: 1.5,
                opacity: 1,
                fillOpacity: 1
            });
        ligarInfo(marcador, est);
        marcador.addTo(layers.structures);

        // Radius circle (only if few structures to not clutter, or always depending on preference)
        // We'll draw them very subtly
        L.circle(latLng, {
            radius: data.kpis.radio,
            color: 'cyan',
            weight: 1,
            fill: false,
            opacity: 0.3
        }).addTo(layers.radii);
    });

    if (mostrarEstructuras) {
        layers.radii.addTo(currentMap);
        layers.structures.addTo(currentMap);
    }

    if (ajustarVista && data.estructuras.length > 0) {
        let encuadre = bounds;
        // Con pocas estructuras se encuadra al radio de busqueda y no al punto.
        // Antes se usaba un zoom fijo de 17, que con el radio en 1000 m dejaba
        // una circunferencia de 1682 px sobre un mapa de 769: quedaba entera
        // fuera de la pantalla y parecia que no se dibujaba.
        const radio = data.kpis.radio || 0;
        if (radio > 0 && data.estructuras.length <= 3) {
            data.estructuras.forEach(e => {
                encuadre = encuadre.extend(L.latLng(e.lat, e.lon).toBounds(radio * 2));
            });
        }
        currentMap.fitBounds(encuadre, { padding: [40, 40] });
    }

    const leyendaCalor = document.getElementById('heatLegend');
    if (mode !== 'heatmap') {
        if (leyendaCalor) leyendaCalor.style.display = 'none';
        ocultarTopPanel();
    }

    // Al filtrar por una estructura concreta se resalta sola, sin esperar un
    // clic: su radio y las descargas que cayeron dentro son justamente lo que
    // se fue a ver. En el mapa de calor esto importa mas, porque ahi no se
    // dibujan los circulos de radio ni los rayos uno por uno. El clic queda
    // solo para abrir el globo con los datos.
    // detalle_estructura llega unicamente cuando se filtro por una estructura,
    // asi que es la senal exacta de "el usuario eligio esta".
    if (data.detalle_estructura) {
        data.estructuras.forEach((est, i) => destacarEstructura(est, data, { limpiar: i === 0 }));
    }

    if (mode === 'general') {
        // Antes el color codificaba la antiguedad del rayo, que no decia nada
        // util. Ahora codifica su corriente: amarillo los mas suaves, rojo los
        // mas intensos, con la escala ajustada a lo que quedo tras los filtros.
        const magnitudes = data.rayos.map(magnitudCorriente);
        const minKA = magnitudes.length ? Math.min(...magnitudes) : 0;
        const maxKA = magnitudes.length ? Math.max(...magnitudes) : 0;
        const rangoKA = maxKA - minKA || 1;

        data.rayos.forEach(r => {
            const t = (magnitudCorriente(r) - minKA) / rangoKA;

            L.marker([r.lat, r.lon], {
                icon: getBoltIcon(interpolarEscala(ESCALA_CORRIENTE, t))
            }).bindPopup(`
                <b>Corriente:</b> ${r.corriente} kA<br>
                <b>Fecha:</b> ${r.fecha}
            `).addTo(layers.strikes);
        });

        layers.strikes.addTo(currentMap);
        renderLeyendaCorriente(minKA, maxKA);

    } else if (mode === 'heatmap') {
        const impactos = data.estructuras.map(e => e.impactos || 0).filter(n => n > 0);
        if (impactos.length === 0) {
            // Sin impactos no hay criticidad que graduar, pero el mapa no puede
            // quedar en blanco: las estructuras ya se dibujaron mas arriba con
            // el mismo estilo del Mapa General.
            configurarUmbral(0, 0);
            renderLeyendaCalor(0, 0);
            ocultarTopPanel();
            return;
        }

        const min = Math.min(...impactos);
        const max = Math.max(...impactos);
        configurarUmbral(min, max);
        dibujarCriticidad(data, min, max);
        renderLeyendaCalor(min, max);
        renderTopPanel(data, min, max);
    }
}

// Cada estructura sigue siendo su propio punto, coloreado y dimensionado segun
// cuantos impactos recibio. Una mancha difusa perderia el detalle por
// estructura, que es justamente lo que se quiere identificar.
function dibujarCriticidad(data, min, max) {
    // Con un solo valor distinto no hay rango que normalizar: todo al tope
    const rango = max - min || 1;
    const umbral = umbralActual();

    const visibles = data.estructuras.filter(e => (e.impactos || 0) >= Math.max(umbral, 1));

    if (typeof L.heatLayer !== 'undefined' && visibles.length > 0) {
        const puntos = visibles.map(e => [e.lat, e.lon, e.impactos]);
        layers.calorFondo = L.heatLayer(puntos, {
            // Radio grande y desenfoque generoso: la mancha tiene que desbordar
            // el punto y fundirse con la de al lado para que se lean zonas de
            // densidad, no un halo por estructura
            radius: 42,
            blur: 30,
            maxZoom: 18,
            // Sin max explicito la libreria normaliza contra 1.0 y satura todo
            max: Math.max(...visibles.map(e => e.impactos)),
            minOpacity: 0.3,
            gradient: { 0.0: '#1d4ed8', 0.35: '#06b6d4', 0.6: '#facc15', 0.8: '#f97316', 1.0: '#dc2626' }
        });
        layers.calorFondo.addTo(currentMap);

        // El plugin cuelga su canvas del overlayPane, donde el SVG de los
        // marcadores ya existe desde que se creo el mapa, asi que la mancha
        // termina encima. En vez de mover el canvas a otro pane (el plugin
        // despues no lo encuentra al removerlo y tira NotFoundError), se manda
        // el SVG al final: dentro del pane manda el orden de insercion.
        const overlay = currentMap.getPane('overlayPane');
        const svg = overlay ? overlay.querySelector('svg') : null;
        if (svg) overlay.appendChild(svg);
    }

    data.estructuras.forEach(est => {
        const n = est.impactos || 0;
        if (n > 0 && n < umbral) return;

        const t = n > 0 ? (n - min) / rango : 0;
        // Las estructuras sin impactos quedan como puntos grises chicos: dejan
        // ver el trazado de la red y donde no paso nada. Con umbral activo
        // estorban, asi que se ocultan
        const sinImpactos = n === 0;
        if (sinImpactos && umbral > min) return;

        // Los porticos conservan su rombo verde en los dos mapas: la forma dice
        // que son y el color los separa del gradiente de criticidad
        if (est.es_portico) {
            const rombo = L.marker([est.lat, est.lon], { icon: getPorticoIcon() });
            ligarInfo(rombo, est);
            if (n > 0) rombo.on('click', () => destacarEstructura(est, data));
            rombo.addTo(layers.strikes);
            return;
        }

        // Las protegidas se pintan del mismo morado con borde blanco que usa el
        // Mapa General: un color significa lo mismo en los dos mapas. Pierden
        // el tono de criticidad, pero el tamano del punto sigue creciendo con
        // los impactos, asi que se ve igual cuales son las mas golpeadas.
        const marcador = L.circleMarker([est.lat, est.lon], {
            radius: sinImpactos ? 3 : 5 + t * 6,
            fillColor: est.protegido ? COLOR_PROTECCION.protegida
                     : (sinImpactos ? '#6b7280' : colorCriticidad(t)),
            color: '#fff',
            weight: sinImpactos ? 1 : (est.protegido ? 2.5 : 1),
            opacity: sinImpactos && !est.protegido ? 0.35 : (est.protegido ? 1 : 0.7),
            fillOpacity: sinImpactos && !est.protegido ? 0.5 : 0.95
        });

        ligarInfo(marcador, est);
        if (n > 0) marcador.on('click', () => destacarEstructura(est, data));
        marcador.addTo(layers.strikes);
    });

    layers.strikes.addTo(currentMap);
}
