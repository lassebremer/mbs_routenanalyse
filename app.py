import os
import json
import math
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
import requests
import folium
import osmnx as ox
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, LineString
from shapely.ops import transform
import pyproj
from io import BytesIO

# Konfiguration importieren
from config import config

# Flask-App initialisieren
app = Flask(__name__)

# Konfiguration laden basierend auf Umgebung
config_name = os.environ.get('FLASK_ENV', 'default')
app.config.from_object(config[config_name])

# Sicherstellen, dass SECRET_KEY gesetzt ist
if not app.config.get('SECRET_KEY'):
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production-12345')

# API-Konfiguration aus App-Config
API_KEY = app.config.get('MAPS_API_KEY')
API_LIMITS = app.config.get('API_LIMITS')
USAGE_FILE = app.config.get('USAGE_FILE')
SEARCH_TERMS = app.config.get('SEARCH_TERMS')

# Globale Variable für gefundene Märkte (für Excel-Export)
found_markets_data = []

# Session-basierte Suchbegriffe (Default aus Config)
def get_search_terms():
    """Gibt die aktuellen Suchbegriffe aus der Session zurück, oder die Standard-Begriffe."""
    from flask import session
    if 'search_terms' not in session:
        session['search_terms'] = SEARCH_TERMS.copy()
    return session['search_terms']

def set_search_terms(terms):
    """Setzt die Suchbegriffe in der Session."""
    from flask import session
    session['search_terms'] = terms
    session.permanent = True  # Session permanent machen

# Geocoding-Funktionen (vereinfacht - nur address)
def geocode_address(api_key, address):
    """Konvertiert eine Adresse zu Koordinaten via Google Geocoding API."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address,
        "key": api_key,
        "language": "de"
    }
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        if data["status"] == "OK" and data["results"]:
            location = data["results"][0]["geometry"]["location"]
            formatted_address = data["results"][0]["formatted_address"]
            return {
                "lat": location["lat"],
                "lng": location["lng"], 
                "formatted_address": formatted_address,
                "status": "OK"
            }
        else:
            return {
                "lat": None,
                "lng": None,
                "formatted_address": None,
                "status": data["status"]
            }
    except Exception as e:
        return {
            "lat": None,
            "lng": None,
            "formatted_address": None,
            "status": "ERROR",
            "error": str(e)
        }

# API-Nutzung-Tracking (ohne autocomplete)
def load_api_usage():
    """Lädt die API-Nutzungsdaten aus der JSON-Datei."""
    if not os.path.exists(USAGE_FILE):
        return {}
    
    try:
        with open(USAGE_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_api_usage(usage_data):
    """Speichert die API-Nutzungsdaten in der JSON-Datei."""
    try:
        with open(USAGE_FILE, 'w') as f:
            json.dump(usage_data, f, indent=2)
    except IOError:
        pass

def check_api_quota(api_type, requests_needed=1):
    """Prüft API-Kontingent."""
    if api_type not in API_LIMITS:
        return False, 0, 0
    
    usage_data = load_api_usage()
    current_month = datetime.now().strftime("%Y-%m")
    
    if current_month not in usage_data:
        usage_data[current_month] = {}
    
    current_usage = usage_data[current_month].get(api_type, 0)
    max_requests = API_LIMITS[api_type]
    
    if current_usage + requests_needed > max_requests:
        return False, current_usage, max_requests - current_usage
    
    return True, current_usage, max_requests - current_usage

def record_api_usage(api_type, requests_count=1):
    """Zeichnet die API-Nutzung auf."""
    if api_type not in API_LIMITS:
        return
    
    usage_data = load_api_usage()
    current_month = datetime.now().strftime("%Y-%m")
    
    if current_month not in usage_data:
        usage_data[current_month] = {}
    
    current_usage = usage_data[current_month].get(api_type, 0)
    usage_data[current_month][api_type] = current_usage + requests_count
    
    save_api_usage(usage_data)

# Routing-Funktionen
def calculate_bearing(lat1, lon1, lat2, lon2):
    """Berechnet die Peilung zwischen zwei Punkten."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1)*math.cos(phi2)*math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def bearing_to_direction(bearing):
    """Konvertiert Peilung zu Himmelsrichtung."""
    if 0 <= bearing < 22.5 or 337.5 <= bearing < 360:
        return "N"
    elif 22.5 <= bearing < 67.5:
        return "NE"
    elif 67.5 <= bearing < 112.5:
        return "E"
    elif 112.5 <= bearing < 157.5:
        return "SE"
    elif 157.5 <= bearing < 202.5:
        return "S"
    elif 202.5 <= bearing < 247.5:
        return "SW"
    elif 247.5 <= bearing < 292.5:
        return "W"
    else:
        return "NW"

def find_places(api_key, lat, lng, radius, keyword):
    """Sucht Märkte via Google Places API."""
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "keyword": keyword,
        "type": "supermarket",
        "key": api_key
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}

def generate_map(festival_lat, festival_lon, radius_km=40, route_radius_km=2, selected_terms=None):
    """Generiert die Karte mit Märkten und Routen - nur Märkte entlang der Anfahrtsrouten."""
    global found_markets_data
    found_markets_data = []  # Reset der gefundenen Märkte
    
    # Verwende ausgewählte Suchbegriffe oder alle verfügbaren
    if selected_terms is None:
        search_terms = get_search_terms()
    else:
        search_terms = selected_terms
    
    if not search_terms:
        return None, "Keine Suchbegriffe ausgewählt"
    
    try:
        radius_m = radius_km * 1000
        ROUTE_BUFFER_M = route_radius_km * 1000  # Puffer um die Route in Metern
        
        # Festival-Punkt
        festival_point_wgs = Point(festival_lon, festival_lat)
        project_wgs_to_merc = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
        project_merc_to_wgs = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
        
        festival_point_merc = transform(project_wgs_to_merc, festival_point_wgs)
        buffered_merc = festival_point_merc.buffer(radius_m)
        circle_polygon = transform(project_merc_to_wgs, buffered_merc)
        
        # OSM-Straßennetz laden
        try:
            G = ox.graph_from_polygon(circle_polygon, network_type='drive')
        except:
            G = ox.graph_from_point((festival_lat, festival_lon), dist=radius_m, network_type='drive')
        
        # Anschlussstellen finden
        highway_values = ["motorway_link", "trunk_link"]
        connections_gdfs = []
        
        for val in highway_values:
            try:
                gdf_features = ox.features_from_polygon(circle_polygon, tags={"highway": val})
                if len(gdf_features) > 0:
                    point_list = []
                    for _, rowf in gdf_features.iterrows():
                        geom = rowf["geometry"]
                        if isinstance(geom, Point):
                            point_list.append(geom)
                        elif hasattr(geom, "centroid"):
                            point_list.append(geom.centroid)
                    
                    if point_list:
                        conn_gdf = gpd.GeoDataFrame(geometry=point_list, crs="EPSG:4326")
                        conn_gdf["conn_type"] = val
                        connections_gdfs.append(conn_gdf)
            except:
                continue
        
        # Fallback für Anschlussstellen
        if not connections_gdfs:
            important_nodes = []
            for node_id, node_data in G.nodes(data=True):
                if G.degree[node_id] >= 3:
                    point = Point(node_data['x'], node_data['y'])
                    important_nodes.append(point)
            
            if important_nodes:
                nodes_gdf = gpd.GeoDataFrame(geometry=important_nodes[:20], crs="EPSG:4326")
                nodes_gdf["conn_type"] = "network_junction"
                connections_gdfs.append(nodes_gdf)
        
        if not connections_gdfs:
            return None, "Keine Anschlussstellen gefunden"
        
        connections_gdf = pd.concat(connections_gdfs, ignore_index=True)
        
        # Distanz zum Festival berechnen
        festival_gdf = gpd.GeoDataFrame(geometry=[festival_point_wgs], crs="EPSG:4326").to_crs(epsg=3857)
        festival_point_merc = festival_gdf.geometry.iloc[0]
        connections_gdf_merc = connections_gdf.to_crs(epsg=3857)
        connections_gdf_merc["distance_m"] = connections_gdf_merc.distance(festival_point_merc)
        connections_gdf = connections_gdf_merc.to_crs(epsg=4326)
        connections_gdf["festival_dist_km"] = connections_gdf_merc["distance_m"] / 1000.0
        
        # Richtungen berechnen
        connections_gdf["bearing"] = connections_gdf.apply(
            lambda row: calculate_bearing(festival_lat, festival_lon, row.geometry.y, row.geometry.x), axis=1)
        connections_gdf["direction"] = connections_gdf["bearing"].apply(bearing_to_direction)
        
        # Pro Richtung: nächste Anschlussstelle auswählen
        eight_dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        selected_exits = []
        for d in eight_dirs:
            subset = connections_gdf[connections_gdf["direction"] == d]
            if len(subset) > 0:
                idxmin = subset["festival_dist_km"].idxmin()
                selected_exits.append(subset.loc[idxmin])
        
        if not selected_exits:
            return None, "Keine Anschlussstellen in den Hauptrichtungen gefunden"
        
        selected_exits_gdf = gpd.GeoDataFrame(selected_exits, geometry="geometry", crs="EPSG:4326")
        
        # Märkte finden
        if not API_KEY:
            return None, "Kein API-Key verfügbar"
        
        # API-Kontingent prüfen
        quota_ok, _, _ = check_api_quota("places", len(search_terms))
        if not quota_ok:
            return None, "API-Limit erreicht"
        
        all_markets = []
        for term in search_terms:
            results = find_places(API_KEY, festival_lat, festival_lon, radius_m, term)
            if results.get("status") == "OK":
                # Keyword zu jedem gefundenen Markt hinzufügen
                for market in results.get("results", []):
                    market["search_keyword"] = term
                all_markets.extend(results.get("results", []))
        
        if all_markets:
            record_api_usage("places", len(search_terms))
        
        # Märkte zu GeoDataFrame konvertieren
        market_points = [Point(store["geometry"]["location"]["lng"], store["geometry"]["location"]["lat"]) for store in all_markets]
        markets_gdf = gpd.GeoDataFrame(all_markets, geometry=market_points, crs="EPSG:4326")
        
        # Routing + Buffering + Markt-Filterung
        festival_node = ox.distance.nearest_nodes(G, festival_lon, festival_lat)
        
        def to_merc(geom):
            return transform(project_wgs_to_merc, geom)
        
        def to_wgs(geom):
            return transform(project_merc_to_wgs, geom)
        
        results_list = []
        
        for idx, row in selected_exits_gdf.iterrows():
            exit_point = row.geometry
            direction = row["direction"]
            exit_node = ox.distance.nearest_nodes(G, exit_point.x, exit_point.y)
            
            try:
                route_node_ids = ox.shortest_path(G, festival_node, exit_node, weight="length")
            except:
                continue
            
            if not route_node_ids or len(route_node_ids) < 2:
                continue
            
            # Route als LineString erstellen
            route_coords = [(G.nodes[node]["x"], G.nodes[node]["y"]) for node in route_node_ids]
            route_line_wgs = LineString(route_coords)
            
            # Route zu Mercator projizieren und buffern
            route_line_merc = to_merc(route_line_wgs)
            corridor_merc = route_line_merc.buffer(ROUTE_BUFFER_M)
            corridor_wgs = to_wgs(corridor_merc)
            
            # Märkte innerhalb des Korridors finden
            inside_corridor = markets_gdf[markets_gdf.geometry.within(corridor_wgs)]
            
            results_list.append({
                "direction": direction,
                "exit_point": exit_point,
                "route_line_wgs": route_line_wgs,
                "corridor_wgs": corridor_wgs,
                "markets_count": len(inside_corridor),
                "markets_df": inside_corridor.copy()
            })
        
        # Karte erstellen
        m = folium.Map(location=[festival_lat, festival_lon], zoom_start=9)
        
        # Festival-Marker
        folium.Marker(
            location=[festival_lat, festival_lon], 
            popup="Festivalort", 
            icon=folium.Icon(color='purple', icon='star')
        ).add_to(m)
        
        # Suchradius
        folium.Circle(
            location=[festival_lat, festival_lon], 
            radius=radius_m, 
            color='black', 
            fill=False, 
            dash_array='5,5', 
            tooltip=f"{radius_km} km Umkreis"
        ).add_to(m)
        
        # Alle Anschlussstellen (grau)
        for _, rowc in connections_gdf.iterrows():
            folium.CircleMarker(
                location=[rowc.geometry.y, rowc.geometry.x], 
                radius=2, 
                color='gray', 
                fill=True, 
                fill_opacity=0.6,
                popup=f"Anschlussstelle: {rowc.get('conn_type', 'unknown')}"
            ).add_to(m)
        
        # Farbzuordnung für Richtungen
        dir_colors = {
            "N": "darkblue", 
            "NE": "blue", 
            "E": "cadetblue", 
            "SE": "green", 
            "S": "darkgreen", 
            "SW": "orange", 
            "W": "red", 
            "NW": "darkred"
        }
        
        # Routen und gefilterte Märkte anzeigen
        for r in results_list:
            direction = r["direction"]
            exit_pt = r["exit_point"]
            route_line = r["route_line_wgs"]
            markets_df = r["markets_df"]
            color = dir_colors.get(direction, "gray")
            
            # Anschlussstelle markieren
            folium.Marker(
                location=[exit_pt.y, exit_pt.x], 
                popup=f"{direction}-Anschlussstelle<br>Märkte: {len(markets_df)}", 
                icon=folium.Icon(color=color, icon='flag')
            ).add_to(m)
            
            # Route zeichnen
            coords_list = [(lat, lon) for lon, lat in route_line.coords]
            folium.PolyLine(
                locations=coords_list, 
                color=color, 
                weight=4, 
                opacity=0.7, 
                tooltip=f"Route Richtung {direction} ({len(markets_df)} Märkte)"
            ).add_to(m)
            
            # Nur Märkte entlang der Route anzeigen und für Export sammeln
            for _, store_row in markets_df.iterrows():
                folium.Marker(
                    location=[store_row.geometry.y, store_row.geometry.x],
                    icon=folium.Icon(color=color, icon='shopping-cart'),
                    popup=f"<b>{store_row.get('name', 'Markt')}</b><br>Richtung: {direction}<br>Adresse: {store_row.get('vicinity', '')}<br>Bewertung: {store_row.get('rating', 'N/A')}"
                ).add_to(m)
                
                # Markt-Daten für Excel-Export sammeln
                market_export_data = {
                    'name': store_row.get('name', 'Unbekannt'),
                    'vicinity': store_row.get('vicinity', 'Keine Adresse verfügbar'),
                    'search_keyword': store_row.get('search_keyword', 'Unbekannt'),
                    'lat': store_row.geometry.y,
                    'lng': store_row.geometry.x
                }
                found_markets_data.append(market_export_data)
        
        # Karte als HTML-String zurückgeben
        map_html = m._repr_html_()
        
        return map_html, None
        
    except Exception as e:
        return None, f"Fehler bei der Kartenerstellung: {str(e)}"

# Flask-Routen (OHNE /api/autocomplete)
@app.route('/')
def index():
    """Startseite der Anwendung."""
    return render_template('index.html')

@app.route('/api/geocode', methods=['POST'])
def geocode():
    """API-Endpunkt für Geocoding (nur address)."""
    if not API_KEY:
        return jsonify({"error": "Kein API-Key verfügbar"}), 400
    
    data = request.get_json()
    if not data or 'address' not in data:
        return jsonify({"error": "Adresse erforderlich"}), 400
    
    # API-Kontingent prüfen
    quota_ok, _, _ = check_api_quota("geocoding", 1)
    if not quota_ok:
        return jsonify({"error": "API-Limit erreicht"}), 429
    
    result = geocode_address(API_KEY, data['address'])
    if result.get("status") == "OK":
        record_api_usage("geocoding", 1)
    
    return jsonify(result)

@app.route('/api/generate_map', methods=['POST'])
def api_generate_map():
    """API-Endpunkt für Kartenerstellung."""
    data = request.get_json()
    if not data or 'lat' not in data or 'lng' not in data:
        return jsonify({"error": "Fehlende Koordinaten"}), 400
    
    lat = float(data['lat'])
    lng = float(data['lng'])
    radius = int(data.get('radius', 40))
    route_radius = float(data.get('route_radius', 2))
    selected_terms = data.get('selected_terms', None)  # Neue Parameter für ausgewählte Suchbegriffe
    
    map_html, error = generate_map(lat, lng, radius, route_radius, selected_terms)
    
    if error:
        return jsonify({"error": error}), 500
    
    return jsonify({"map": map_html})

@app.route('/api/search_terms', methods=['GET'])
def api_get_search_terms():
    """API-Endpunkt zum Abrufen der aktuellen Suchbegriffe."""
    terms = get_search_terms()
    return jsonify({"search_terms": terms})

@app.route('/api/search_terms', methods=['POST'])
def api_add_search_term():
    """API-Endpunkt zum Hinzufügen eines neuen Suchbegriffs."""
    data = request.get_json()
    if not data or 'term' not in data:
        return jsonify({"error": "Suchbegriff erforderlich"}), 400
    
    new_term = data['term'].strip()
    if not new_term:
        return jsonify({"error": "Leerer Suchbegriff"}), 400
    
    terms = get_search_terms()
    if new_term not in terms:
        terms.append(new_term)
        set_search_terms(terms)
        return jsonify({"success": True, "message": f"Suchbegriff '{new_term}' hinzugefügt", "search_terms": terms})
    else:
        return jsonify({"error": f"Suchbegriff '{new_term}' bereits vorhanden"}), 400

@app.route('/api/search_terms/<int:index>', methods=['DELETE'])
def api_remove_search_term(index):
    """API-Endpunkt zum Entfernen eines Suchbegriffs."""
    terms = get_search_terms()
    if 0 <= index < len(terms):
        removed_term = terms.pop(index)
        set_search_terms(terms)
        return jsonify({"success": True, "message": f"Suchbegriff '{removed_term}' entfernt", "search_terms": terms})
    else:
        return jsonify({"error": "Ungültiger Index"}), 400

@app.route('/api/search_terms/reset', methods=['POST'])
def api_reset_search_terms():
    """API-Endpunkt zum Zurücksetzen der Suchbegriffe auf die Standardwerte."""
    set_search_terms(SEARCH_TERMS.copy())
    return jsonify({"success": True, "message": "Suchbegriffe zurückgesetzt", "search_terms": get_search_terms()})

@app.route('/api/stats')
def api_stats():
    """API-Endpunkt für Nutzungsstatistiken (ohne autocomplete)."""
    usage_data = load_api_usage()
    current_month = datetime.now().strftime("%Y-%m")
    
    if current_month not in usage_data:
        usage_data[current_month] = {}
    
    current_month_data = usage_data[current_month]
    
    stats = {}
    for api_type, max_requests in API_LIMITS.items():
        current_usage = current_month_data.get(api_type, 0)
        remaining = max_requests - current_usage
        usage_percent = (current_usage / max_requests) * 100
        
        stats[api_type] = {
            "current_usage": current_usage,
            "max_requests": max_requests,
            "remaining": remaining,
            "usage_percent": round(usage_percent, 1)
        }
    
    return jsonify({
        "current_month": current_month,
        "apis": stats
    })

@app.route('/api/export_markets')
def export_markets():
    """API-Endpunkt für Excel-Export der gefundenen Märkte."""
    global found_markets_data
    
    if not found_markets_data:
        return jsonify({"error": "Keine Märkte zum Exportieren verfügbar. Erstellen Sie zuerst eine Karte."}), 400
    
    try:
        # DataFrame aus den gesammelten Daten erstellen
        df = pd.DataFrame(found_markets_data)
        
        # Nur die gewünschten Spalten für den Export auswählen
        export_df = df[['name', 'vicinity', 'search_keyword']].copy()
        export_df.columns = ['Marktname', 'Adresse', 'Suchbegriff']
        
        # Duplikate entfernen (falls ein Markt mit mehreren Keywords gefunden wurde)
        export_df = export_df.drop_duplicates(subset=['Marktname', 'Adresse'])
        
        # Excel-Datei in Memory erstellen
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            export_df.to_excel(writer, sheet_name='LEH_Export', index=False)
            
            # Spaltenbreite anpassen
            worksheet = writer.sheets['LEH_Export']
            for column in worksheet.columns:
                max_length = 0
                column = [cell for cell in column]
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = (max_length + 2) * 1.2
                worksheet.column_dimensions[column[0].column_letter].width = adjusted_width
        
        output.seek(0)
        
        # Aktuelles Datum für Dateinamen
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"LEH_Export_{date_str}.xlsx"
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        return jsonify({"error": f"Fehler beim Erstellen der Excel-Datei: {str(e)}"}), 500

if __name__ == '__main__':
    print("🌟 Festival Märkte Finder - Einfache Version")
    print("=" * 50)
    print("📍 URL: http://localhost:5000")
    print("🛑 Zum Beenden: Strg+C")
    print("✅ Ohne Autocomplete - nur direkte Adresseingabe")
    print("=" * 50)
    app.run(debug=True, host='0.0.0.0', port=5000) 