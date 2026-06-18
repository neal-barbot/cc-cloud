CREATE TABLE user_snapshots (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id VARCHAR(64) NOT NULL,
  version INT NOT NULL,
  storage_key VARCHAR(256) NOT NULL,
  file_count INT,
  total_size BIGINT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_user_version (user_id, version DESC)
);

CREATE TABLE sandbox_assignments (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id VARCHAR(64) NOT NULL,
  sandbox_id VARCHAR(128) NOT NULL,
  endpoint VARCHAR(256) NOT NULL,
  status VARCHAR(32) NOT NULL,
  snapshot_version INT,
  last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_user_active (user_id, status),
  INDEX idx_sandbox_status (status, last_seen_at)
);

CREATE TABLE sandbox_events (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id VARCHAR(64) NOT NULL,
  sandbox_id VARCHAR(128) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  payload JSON,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_user_created (user_id, created_at)
);
