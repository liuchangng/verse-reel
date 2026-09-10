-- 古诗词短视频工厂 数据库 schema（SQLite，poems.db）
-- 由 scripts/export_schema.py 从 SQLAlchemy 模型自动生成，勿手改。
-- 共 9 张表：hotspots, poem_tags, poems, poets, system_settings, poem_terms, tasks, generation_jobs, scripts

-- ===== 表 hotspots =====
CREATE TABLE hotspots (
	id INTEGER NOT NULL, 
	platform VARCHAR(32), 
	title VARCHAR(255), 
	hot INTEGER, 
	url VARCHAR(512), 
	recommended_poems TEXT, 
	fetched_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_hotspots_title ON hotspots (title);

CREATE INDEX ix_hotspots_platform ON hotspots (platform);

-- ===== 表 poem_tags =====
CREATE TABLE poem_tags (
	id INTEGER NOT NULL, 
	poem_id INTEGER NOT NULL, 
	tag VARCHAR(32) NOT NULL, 
	tag_type VARCHAR(16) NOT NULL, 
	confidence INTEGER NOT NULL, 
	source VARCHAR(32) NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);

-- ===== 表 poems =====
CREATE TABLE poems (
	id INTEGER NOT NULL, 
	cnk_id INTEGER, 
	title VARCHAR(200) NOT NULL, 
	title_traditional VARCHAR(200), 
	author VARCHAR(100), 
	author_traditional VARCHAR(100), 
	dynasty VARCHAR(50), 
	genre VARCHAR(50), 
	content TEXT, 
	content_traditional TEXT, 
	rhyme VARCHAR(20), 
	group_id INTEGER, 
	quality_score INTEGER, 
	translation TEXT, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_poems_group_id ON poems (group_id);

CREATE INDEX ix_poems_quality_score ON poems (quality_score);

CREATE UNIQUE INDEX ix_poems_cnk_id ON poems (cnk_id);

-- ===== 表 poets =====
CREATE TABLE poets (
	id INTEGER NOT NULL, 
	author VARCHAR(100) NOT NULL, 
	author_variants TEXT, 
	dynasty VARCHAR(50) NOT NULL, 
	dynasty_raw TEXT, 
	total_rows INTEGER, 
	prose_rows INTEGER, 
	genre_stats TEXT, 
	split_rows INTEGER, 
	tribute_hits INTEGER, 
	social_poems INTEGER, 
	official_rank INTEGER, 
	family_lineage INTEGER, 
	anthology_hits INTEGER, 
	textbook_hits INTEGER, 
	fame_score FLOAT, 
	fame_level VARCHAR(2), 
	author_category VARCHAR(20), 
	school_tags TEXT, 
	style_tags TEXT, 
	life_tags TEXT, 
	fact_tags TEXT, 
	src VARCHAR(200), 
	updated_at VARCHAR(32), 
	PRIMARY KEY (id)
);

CREATE INDEX idx_poets_dynasty ON poets (dynasty);

CREATE INDEX idx_poets_author ON poets (author);

CREATE INDEX idx_poets_fame ON poets (fame_score);

-- ===== 表 system_settings =====
CREATE TABLE system_settings (
	id INTEGER NOT NULL, 
	scope VARCHAR(32) NOT NULL, 
	config_json TEXT NOT NULL, 
	version INTEGER, 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (scope)
);

-- ===== 表 poem_terms =====
CREATE TABLE poem_terms (
	id INTEGER NOT NULL, 
	poem_id INTEGER NOT NULL, 
	term VARCHAR(64) NOT NULL, 
	position VARCHAR(1) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(poem_id) REFERENCES poems (id)
);

CREATE INDEX idx_poem_terms_term ON poem_terms (term);

-- ===== 表 tasks =====
CREATE TABLE tasks (
	id INTEGER NOT NULL, 
	poem_id INTEGER NOT NULL, 
	source_hotspot_title VARCHAR(200), 
	source_keywords TEXT, 
	status VARCHAR(20), 
	current_stage VARCHAR(20), 
	progress INTEGER, 
	platform VARCHAR(20), 
	platforms TEXT, 
	platform_outputs TEXT, 
	script TEXT, 
	script_score INTEGER, 
	storyboard TEXT, 
	style VARCHAR(30), 
	voice_preset VARCHAR(32), 
	character_description TEXT, 
	image_score INTEGER, 
	video_url VARCHAR(500), 
	video_duration INTEGER, 
	image_urls TEXT, 
	character_ref VARCHAR(500), 
	audio_url VARCHAR(500), 
	subtitle_url VARCHAR(500), 
	error_message TEXT, 
	review_status VARCHAR(20), 
	review_comment TEXT, 
	reviewed_at DATETIME, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(poem_id) REFERENCES poems (id)
);

-- ===== 表 generation_jobs =====
CREATE TABLE generation_jobs (
	id INTEGER NOT NULL, 
	task_id INTEGER NOT NULL, 
	stage VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	priority INTEGER, 
	payload TEXT, 
	attempts INTEGER, 
	attempts_log TEXT, 
	last_error TEXT, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES tasks (id)
);

CREATE INDEX ix_generation_jobs_task_id ON generation_jobs (task_id);

CREATE INDEX ix_jobs_status_priority ON generation_jobs (status, priority);

CREATE INDEX ix_generation_jobs_status ON generation_jobs (status);

-- ===== 表 scripts =====
CREATE TABLE scripts (
	id INTEGER NOT NULL, 
	task_id INTEGER NOT NULL, 
	hook TEXT, 
	rebrand TEXT, 
	details TEXT, 
	alignment TEXT, 
	emotion TEXT, 
	full_script TEXT, 
	score INTEGER, 
	score_feedback TEXT, 
	retry_count INTEGER, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES tasks (id)
);
