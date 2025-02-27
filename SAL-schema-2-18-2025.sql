--
-- PostgreSQL database dump
--

-- Dumped from database version 17.3
-- Dumped by pg_dump version 17.3

-- Started on 2025-02-18 10:40:37

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 230 (class 1259 OID 16541)
-- Name: books; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".books (
    books_id integer NOT NULL,
    book_name character varying(255) NOT NULL
);


ALTER TABLE "SAL-schema".books OWNER TO postgres;

--
-- TOC entry 229 (class 1259 OID 16540)
-- Name: books_books_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".books_books_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".books_books_id_seq OWNER TO postgres;

--
-- TOC entry 4984 (class 0 OID 0)
-- Dependencies: 229
-- Name: books_books_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".books_books_id_seq OWNED BY "SAL-schema".books.books_id;


--
-- TOC entry 222 (class 1259 OID 16447)
-- Name: event_types; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".event_types (
    event_type_id integer NOT NULL,
    type_name character varying(50) NOT NULL
);


ALTER TABLE "SAL-schema".event_types OWNER TO postgres;

--
-- TOC entry 221 (class 1259 OID 16446)
-- Name: event_types_event_type_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".event_types_event_type_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".event_types_event_type_id_seq OWNER TO postgres;

--
-- TOC entry 4985 (class 0 OID 0)
-- Dependencies: 221
-- Name: event_types_event_type_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".event_types_event_type_id_seq OWNED BY "SAL-schema".event_types.event_type_id;


--
-- TOC entry 228 (class 1259 OID 16474)
-- Name: events; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".events (
    event_id integer NOT NULL,
    event_name character varying(255) NOT NULL,
    commence_time timestamp with time zone NOT NULL,
    end_time timestamp with time zone,
    sport_id integer NOT NULL,
    event_type_id integer NOT NULL,
    location_id integer NOT NULL,
    weather_id integer NOT NULL,
    participant_1_id integer,
    participant_2_id integer
);


ALTER TABLE "SAL-schema".events OWNER TO postgres;

--
-- TOC entry 227 (class 1259 OID 16473)
-- Name: events_event_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".events_event_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".events_event_id_seq OWNER TO postgres;

--
-- TOC entry 4986 (class 0 OID 0)
-- Dependencies: 227
-- Name: events_event_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".events_event_id_seq OWNED BY "SAL-schema".events.event_id;


--
-- TOC entry 224 (class 1259 OID 16456)
-- Name: locations; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".locations (
    location_id integer NOT NULL,
    location_name character varying(255) NOT NULL,
    address text,
    city character varying(100),
    state character varying(100),
    country character varying(100)
);


ALTER TABLE "SAL-schema".locations OWNER TO postgres;

--
-- TOC entry 223 (class 1259 OID 16455)
-- Name: locations_location_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".locations_location_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".locations_location_id_seq OWNER TO postgres;

--
-- TOC entry 4987 (class 0 OID 0)
-- Dependencies: 223
-- Name: locations_location_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".locations_location_id_seq OWNED BY "SAL-schema".locations.location_id;


--
-- TOC entry 234 (class 1259 OID 16555)
-- Name: odds; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".odds (
    odds_id integer NOT NULL,
    event_id integer NOT NULL,
    books_id integer NOT NULL,
    wager_type_id integer NOT NULL,
    last_update timestamp with time zone NOT NULL,
    price integer NOT NULL,
    point numeric(5,2)
);


ALTER TABLE "SAL-schema".odds OWNER TO postgres;

--
-- TOC entry 233 (class 1259 OID 16554)
-- Name: odds_odds_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".odds_odds_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".odds_odds_id_seq OWNER TO postgres;

--
-- TOC entry 4988 (class 0 OID 0)
-- Dependencies: 233
-- Name: odds_odds_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".odds_odds_id_seq OWNED BY "SAL-schema".odds.odds_id;


--
-- TOC entry 236 (class 1259 OID 16577)
-- Name: participants; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".participants (
    participant_id integer NOT NULL,
    participant_name character varying(255) NOT NULL,
    participant_type character varying(50) NOT NULL,
    sport_id integer NOT NULL
);


ALTER TABLE "SAL-schema".participants OWNER TO postgres;

--
-- TOC entry 235 (class 1259 OID 16576)
-- Name: participants_participant_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".participants_participant_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".participants_participant_id_seq OWNER TO postgres;

--
-- TOC entry 4989 (class 0 OID 0)
-- Dependencies: 235
-- Name: participants_participant_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".participants_participant_id_seq OWNED BY "SAL-schema".participants.participant_id;


--
-- TOC entry 220 (class 1259 OID 16436)
-- Name: sports; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".sports (
    sport_id integer NOT NULL,
    sport_key character varying(50) NOT NULL,
    sport_title character varying(100) NOT NULL,
    description text
);


ALTER TABLE "SAL-schema".sports OWNER TO postgres;

--
-- TOC entry 219 (class 1259 OID 16435)
-- Name: sports_sport_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".sports_sport_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".sports_sport_id_seq OWNER TO postgres;

--
-- TOC entry 4990 (class 0 OID 0)
-- Dependencies: 219
-- Name: sports_sport_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".sports_sport_id_seq OWNED BY "SAL-schema".sports.sport_id;


--
-- TOC entry 232 (class 1259 OID 16548)
-- Name: wager_types; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".wager_types (
    wager_type_id integer NOT NULL,
    wager_type character varying(255) NOT NULL
);


ALTER TABLE "SAL-schema".wager_types OWNER TO postgres;

--
-- TOC entry 231 (class 1259 OID 16547)
-- Name: wager_types_wager_type_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".wager_types_wager_type_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".wager_types_wager_type_id_seq OWNER TO postgres;

--
-- TOC entry 4991 (class 0 OID 0)
-- Dependencies: 231
-- Name: wager_types_wager_type_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".wager_types_wager_type_id_seq OWNED BY "SAL-schema".wager_types.wager_type_id;


--
-- TOC entry 226 (class 1259 OID 16465)
-- Name: weather_forecasts; Type: TABLE; Schema: SAL-schema; Owner: postgres
--

CREATE TABLE "SAL-schema".weather_forecasts (
    weather_id integer NOT NULL,
    forecast_time timestamp with time zone NOT NULL,
    temperature numeric(4,1),
    precipitation_percent integer,
    wind_speed numeric(4,1),
    additional_info text
);


ALTER TABLE "SAL-schema".weather_forecasts OWNER TO postgres;

--
-- TOC entry 225 (class 1259 OID 16464)
-- Name: weather_forecasts_weather_id_seq; Type: SEQUENCE; Schema: SAL-schema; Owner: postgres
--

CREATE SEQUENCE "SAL-schema".weather_forecasts_weather_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "SAL-schema".weather_forecasts_weather_id_seq OWNER TO postgres;

--
-- TOC entry 4992 (class 0 OID 0)
-- Dependencies: 225
-- Name: weather_forecasts_weather_id_seq; Type: SEQUENCE OWNED BY; Schema: SAL-schema; Owner: postgres
--

ALTER SEQUENCE "SAL-schema".weather_forecasts_weather_id_seq OWNED BY "SAL-schema".weather_forecasts.weather_id;


--
-- TOC entry 4780 (class 2604 OID 16544)
-- Name: books books_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".books ALTER COLUMN books_id SET DEFAULT nextval('"SAL-schema".books_books_id_seq'::regclass);


--
-- TOC entry 4776 (class 2604 OID 16450)
-- Name: event_types event_type_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".event_types ALTER COLUMN event_type_id SET DEFAULT nextval('"SAL-schema".event_types_event_type_id_seq'::regclass);


--
-- TOC entry 4779 (class 2604 OID 16477)
-- Name: events event_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".events ALTER COLUMN event_id SET DEFAULT nextval('"SAL-schema".events_event_id_seq'::regclass);


--
-- TOC entry 4777 (class 2604 OID 16459)
-- Name: locations location_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".locations ALTER COLUMN location_id SET DEFAULT nextval('"SAL-schema".locations_location_id_seq'::regclass);


--
-- TOC entry 4782 (class 2604 OID 16558)
-- Name: odds odds_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".odds ALTER COLUMN odds_id SET DEFAULT nextval('"SAL-schema".odds_odds_id_seq'::regclass);


--
-- TOC entry 4783 (class 2604 OID 16580)
-- Name: participants participant_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".participants ALTER COLUMN participant_id SET DEFAULT nextval('"SAL-schema".participants_participant_id_seq'::regclass);


--
-- TOC entry 4775 (class 2604 OID 16439)
-- Name: sports sport_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".sports ALTER COLUMN sport_id SET DEFAULT nextval('"SAL-schema".sports_sport_id_seq'::regclass);


--
-- TOC entry 4781 (class 2604 OID 16551)
-- Name: wager_types wager_type_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".wager_types ALTER COLUMN wager_type_id SET DEFAULT nextval('"SAL-schema".wager_types_wager_type_id_seq'::regclass);


--
-- TOC entry 4778 (class 2604 OID 16468)
-- Name: weather_forecasts weather_id; Type: DEFAULT; Schema: SAL-schema; Owner: postgres
--

ALTER TABLE ONLY "SAL-schema".weather_forecasts ALTER COLUMN weather_id SET DEFAULT nextval('"SAL-schema".weather_forecasts_weather_id_seq'::regclass);


-- Completed on 2025-02-18 10:40:37

--
-- PostgreSQL database dump complete
--

