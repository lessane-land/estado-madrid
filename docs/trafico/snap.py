import re, json, math, sys
pm=open('/tmp/trf/pm.xml',encoding='utf-8-sig').read()
fecha=re.search(r'<fecha_hora>(.*?)</fecha_hora>',pm).group(1)
def g(b,t):
    m=re.search(rf'<{t}>(.*?)</{t}>',b,re.S); return m.group(1).strip() if m else ''
S=[]
for b in re.findall(r'<pm>(.*?)</pm>',pm,re.S):
    x=g(b,'st_x'); y=g(b,'st_y')
    if not x: continue
    def n(v):
        try: return int(v)
        except: return -1
    S.append([round(float(x.replace(',','.'))),round(float(y.replace(',','.'))),n(g(b,'carga')),n(g(b,'intensidad')),n(g(b,'nivelServicio')),1 if g(b,'error')=='S' else 0,g(b,'descripcion') or g(b,'nombre')])
inc=open('/tmp/trf/inc.xml',encoding='utf-8-sig').read()
I=[]
for b in re.findall(r'<Incidencia>(.*?)</Incidencia>',inc,re.S):
    import html
    I.append([round(float(g(b,'utm_x'))),round(float(g(b,'utm_y'))),html.unescape(g(b,'nom_tipo_incidencia')),html.unescape(g(b,'descripcion')),g(b,'fh_inicio')[:16],g(b,'fh_final')[:16],g(b,'es_accidente')=='S',g(b,'es_obras')=='S'])
# UTM 30N forward for landmarks
def utm(lat,lon,zone=30):
    a=6378137.0; f=1/298.257222101; e2=f*(2-f); k0=0.9996
    lon0=math.radians((zone-1)*6-180+3); la=math.radians(lat); lo=math.radians(lon)
    N=a/math.sqrt(1-e2*math.sin(la)**2); T=math.tan(la)**2; C=e2/(1-e2)*math.cos(la)**2; A=math.cos(la)*(lo-lon0)
    e4=e2*e2; e6=e4*e2
    M=a*((1-e2/4-3*e4/64-5*e6/256)*la-(3*e2/8+3*e4/32+45*e6/1024)*math.sin(2*la)+(15*e4/256+45*e6/1024)*math.sin(4*la)-(35*e6/3072)*math.sin(6*la))
    ep2=e2/(1-e2)
    x=k0*N*(A+(1-T+C)*A**3/6+(5-18*T+T*T+72*C-58*ep2)*A**5/120)+500000
    y=k0*(M+N*math.tan(la)*(A*A/2+(5-T+9*C+4*C*C)*A**4/24+(61-58*T+T*T+600*C-330*ep2)*A**6/720))
    return round(x),round(y)
L=[(n,)+utm(a,b) for n,a,b in [('Sol',40.4169,-3.7035),('Atocha',40.4066,-3.6892),('Plaza de Castilla',40.4669,-3.6890),('Moncloa',40.4351,-3.7190),('Nuevos Ministerios',40.4462,-3.6918),('Legazpi',40.3911,-3.6953),('Ventas',40.4307,-3.6634),('Príncipe Pío',40.4214,-3.7208)]]
print(L[0], 'sensors',len(S),'inc',len(I), file=sys.stderr)
json.dump({'fecha':fecha,'s':S,'i':I,'l':L},open('snap.json','w'),ensure_ascii=False,separators=(',',':'))
