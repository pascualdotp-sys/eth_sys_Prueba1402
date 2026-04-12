import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
import os

# ================= CONFIGURACIÓN DE PÁGINA =================
st.set_page_config(page_title="Simulador Avanzado - Quelixtia Engineering", layout="wide")

# ================= CLASE ECONÓMICA (TEA) =================
class TEA_Didactico(bst.TEA):
    def _DPI(self, installed_equipment_cost):
        return self.purchase_cost

    def _TDC(self, DPI):
        return DPI

    def _FCI(self, TDC):
        return self.purchase_cost * self.lang_factor

    def _TCI(self, FCI):
        return FCI + self.WC

    def _FOC(self, FCI):
        return 0.0

    @property
    def VOC(self):
        mat = getattr(self.system, "material_cost", 0)
        util = getattr(self.system, "utility_cost", 0)
        return mat + util

# ================= FUNCIONES DE APOYO =================
def generar_reporte(sistema):
    datos_mat = []
    for s in sistema.streams:
        if s.F_mass > 0:
            datos_mat.append({
                "ID Corriente": s.ID,
                "Temp(°C)": f"{s.T-273.15:.2f}",
                "Presion(bar)": f"{s.P/1e5:.2f}",
                "Flujo(kg/h)": f"{s.F_mass:.2f}",
                "% Etanol": f"{s.imass['Ethanol']/s.F_mass:.1%}",
                "% Agua": f"{s.imass['Water']/s.F_mass:.1%}"
            })
    df_mat = pd.DataFrame(datos_mat).set_index("ID Corriente")

    datos_en = []
    for u in sistema.units:
        calor_kw = 0.0
        tipo_servicio = "-"
        try:
            if isinstance(u, bst.HXprocess):
                calor_kw = (u.outs[0].H - u.ins[0].H) / 3600
                tipo_servicio = "Recuperacion Interna"
            elif isinstance(u, bst.Flash):
                calor_kw = getattr(u, 'Hnet', 0) / 3600
                tipo_servicio = "Separación Flash"
            elif hasattr(u, "duty") and u.duty is not None:
                calor_kw = u.duty / 3600
                if calor_kw > 0.01: tipo_servicio = "Calentamiento (Vapor)"
                if calor_kw < -0.01: tipo_servicio = "Enfriamiento (Agua)"
        except Exception:
            calor_kw = 0.0

        potencia = getattr(u.power_utility, 'rate', 0.0) if hasattr(u, "power_utility") and u.power_utility else 0.0

        if abs(calor_kw) > 0.01:
            datos_en.append({"ID Equipo": u.ID, "Función": tipo_servicio, "Energia Térmica (kw)": f"{calor_kw:.2f}"})
        if potencia > 0.01:
            datos_en.append({"ID Equipo": u.ID, "Función": "Motor bomba", "Energia electrica (kw)": f"{potencia:.2f}"})

    df_en = pd.DataFrame(datos_en).set_index("ID Equipo")
    return df_mat, df_en

def ejecutar_simulacion(agua_feed, etanol_feed, temp_feed, temp_w220, presion_v100_pa, 
                        precio_luz, precio_vapor, precio_agua, precio_mosto, precio_etanol):
    
    # 1. Limpieza y Configuración
    bst.main_flowsheet.clear()
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)
    
    # 2. Corrientes
    mosto = bst.Stream("1-MOSTO", Water=agua_feed, Ethanol=etanol_feed, units="kg/h", T=temp_feed + 273.15, P=101325)
    vinazas_retorno = bst.Stream("Vinazas-Retorno", Water=200, Ethanol=0, units="kg/h", T=95 + 273.15, P=300000)
    
    # 3. Equipos
    P100 = bst.Pump("P-100", ins=mosto, P=4*101325)
    W210 = bst.HXprocess("W-210", ins=(P100-0, vinazas_retorno), outs=("3-Mosto-Pre", "Drenaje"), phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15
    W220 = bst.HXutility("W-220", ins=W210-0, outs="Mezcla", T=temp_w220 + 273.15)
    V100 = bst.IsenthalpicValve("V-100", ins=W220-0, outs="Mezcla-Bifasica", P=presion_v100_pa)
    V1 = bst.Flash("V-1", ins=V100-0, outs=("Vapor caliente", "Vinazas"), P=presion_v100_pa, Q=0)
    W310 = bst.HXutility("W-310", ins=V1-0, outs="Producto Final", T=25 + 273.15)
    P200 = bst.Pump("P-200", ins=V1-1, outs=vinazas_retorno, P=3*101325)
    
    # 4. Simulación Técnica
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    eth_sys.simulate()

    # 5. Aplicar Precios
    bst.PowerUtility.price = precio_luz
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua
    mosto.price = precio_mosto
    eth_sys.simulate()

    producto = W310.outs[0]
    if producto.F_mass <= 0.001:
        raise ValueError("Flujo de producto insuficiente para análisis. Ajusta T o P.")

    # 6. Análisis Económico
    tea = TEA_Didactico(
        system=eth_sys, IRR=0.15, duration=(2025, 2045), income_tax=0.3, depreciation="MACRS7",
        construction_schedule=(0.4, 0.6), startup_months=6, operating_days=330, lang_factor=4.0, WC_over_FCI=0.05
    )

    tea.IRR = 0.0
    costo_produccion = tea.solve_price(producto)
    tea.IRR = 0.15
    precio_venta_sugerido = tea.solve_price(producto)
    producto.price = precio_etanol # Precio real para indicadores actuales

    datos_producto = {
        "T": producto.T - 273.15, "P": producto.P / 101325,
        "F": producto.F_mass, "C": (producto.imass["Ethanol"]/producto.F_mass)*100
    }
    datos_tea = {
        "Costo": costo_produccion, "Sugerido": precio_venta_sugerido,
        "NPV": tea.NPV, "PBP": tea.PBP, "ROI": tea.ROI
    }

    df_mat, df_en = generar_reporte(eth_sys)
    diagrama = "pfd_simulacion"
    eth_sys.diagram(file=diagrama, format="png")
    
    return df_mat, df_en, datos_producto, datos_tea, diagrama + ".png"

# ================= INTERFAZ STREAMLIT =================
st.title("🏭 Planta de Etanol: Simulación e Ingeniería Económica")

# --- SIDEBAR: PARÁMETROS ---
st.sidebar.header("⚙️ Operación")
t_f = st.sidebar.slider("Temp. Alimentación (°C)", 10, 80, 25)
t_w = st.sidebar.slider("Temp. Salida W-220 (°C)", 80, 120, 92)
p_v = st.sidebar.slider("Presión Separador (atm)", 0.1, 3.0, 1.0)

st.sidebar.header("💰 Economía")
p_luz = st.sidebar.slider("Luz ($/kWh)", 0.01, 0.20, 0.085, 0.005)
p_vap = st.sidebar.slider("Vapor ($/MJ)", 0.01, 0.10, 0.025, 0.005)
p_agu = st.sidebar.slider("Agua ($/MJ)", 0.0001, 0.0050, 0.0005, 0.0001)
p_mos = st.sidebar.slider("Mosto ($/kg)", 0.0000001, 0.0000100, 0.0000005, 0.0000001, format="%.7f")
p_eta = st.sidebar.slider("Precio Etanol ($/kg)", 0.5, 3.0, 1.2, 0.1)

# --- EJECUCIÓN ---
if st.sidebar.button("🚀 Ejecutar Simulación"):
    try:
        res = ejecutar_simulacion(900, 100, t_f, t_w, p_v*101325, p_luz, p_vap, p_agu, p_mos, p_eta)
        st.session_state['sim_data'] = res
    except Exception as e:
        st.error(f"Error: {e}")

# --- DISPLAY DE RESULTADOS ---
if 'sim_data' in st.session_state:
    df_mat, df_en, p_info, t_info, img = st.session_state['sim_data']
    
    # Recuadros de Producto
    st.subheader("📦 Producto Final")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Temperatura", f"{p_info['T']:.1f} °C")
    c2.metric("Presión", f"{p_info['P']:.2f} atm")
    c3.metric("Flujo", f"{p_info['F']:.1f} kg/h")
    c4.metric("Pureza Etanol", f"{p_info['C']:.1f} %")

    # Recuadros TEA
    st.subheader("📈 Indicadores Financieros")
    t1, t2, t3, t4, t5 = st.columns(5)
    t1.metric("Costo Real", f"${t_info['Costo']:.2f}/kg")
    t2.metric("Venta Sugerida", f"${t_info['Sugerido']:.2f}/kg")
    t3.metric("NPV (USD)", f"${t_info['NPV']:,.0f}")
    t4.metric("Payback", f"{t_info['PBP']:.1f} años")
    t5.metric("ROI", f"{t_info['ROI']:.1f} %")

    # Tablas
    col_a, col_b = st.columns(2)
    col_a.write("**Balance de Materia**")
    col_a.dataframe(df_mat, use_container_width=True)
    col_b.write("**Balance de Energía**")
    col_b.dataframe(df_en, use_container_width=True)

    # Diagramas
    st.image(img, caption="Diagrama generado por BioSTEAM")

# --- SECCIÓN ISO (AUTOCAD) ---
st.divider()
st.subheader("📐 Planos ISO (AutoCAD Plant 3D)")
cp1, cp2 = st.columns(2)
with cp1:
    st.markdown("**1. Diagrama de Bloques ISO**")
    if os.path.exists("diagrama_bloques.pdf"):
        with open("diagrama_bloques.pdf", "rb") as f:
            st.download_button("📥 Descargar DB", f, "Bloques_ISO.pdf", "application/pdf")
    else: st.info("Sube diagrama_bloques.pdf")
with cp2:
    st.markdown("**2. Avance PFD ISO**")
    if os.path.exists("diagrama_flujo.pdf"):
        with open("diagrama_flujo.pdf", "rb") as f:
            st.download_button("📥 Descargar DFP", f, "PFD_ISO.pdf", "application/pdf")
    else: st.info("Sube diagrama_flujo.pdf")

# ================= MODALIDAD TUTOR IA =================
st.divider()
st.subheader("🤖 Tutor de Ingeniería Química")

habilitar_tutor = st.toggle("Habilitar Modo Tutor con IA")

if habilitar_tutor:
    # Configuración Gemini
    try:
        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        if "messages" not in st.session_state:
            st.session_state.messages = []

        # Ventana de Chat
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        if prompt := st.chat_input("Pregúntale al tutor sobre el proceso..."):
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            # Preparar Contexto de Simulación
            contexto = "Contexto: No hay simulación ejecutada aún."
            if 'sim_data' in st.session_state:
                df_m, df_e, pi, ti, _ = st.session_state['sim_data']
                contexto = f"""
                Simulación actual:
                - Producto: {pi['F']:.1f} kg/h a {pi['C']:.1f}% pureza.
                - Economía: NPV=${ti['NPV']:,.0f}, ROI={ti['ROI']:.1f}%, Payback={ti['PBP']:.1f} años.
                - Costo producción: ${ti['Costo']:.2f}/kg.
                - Balances: {df_m.to_string()}
                """

            full_prompt = f"Eres un experto en Ingeniería Química y BioSTEAM. {contexto}\nUsuario dice: {prompt}"
            
            with st.chat_message("assistant"):
                response = model.generate_content(full_prompt)
                st.markdown(response.text)
                st.session_state.messages.append({"role": "assistant", "content": response.text})
                
    except Exception as e:
        st.error("Configura GEMINI_API_KEY en Streamlit Secrets.")
else:
    st.info("Activa el interruptor superior para hablar con el tutor IA.")
