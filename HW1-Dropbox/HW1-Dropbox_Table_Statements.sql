Create Table Users (
    user_id INT PRIMARY KEY,
    username VARCHAR(255) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    pass_word VARCHAR(255) NOT NULL, --i think "password" is a keyword
    created_at TIMESTAMP
)

Create Table Directories (
    dir_id INT PRIMARY KEY,
    dir_name VARCHAR(255) NOT NULL,
    parent_dir_id INT,
    owner_id INT NOT NULL,
    created_at TIMESTAMP,
    last_modified TIMESTAMP,
    FOREIGN KEY (parent_dir_id) REFERENCES Directories(dir_id)
)

Create Table Files (
    file_id INT PRIMARY KEY,
    file_name VARCHAR(255) NOT NULL,
    file_size INT NOT NULL,
    dir_id INT NOT NULL,
    owner_id INT NOT NULL,
    created_at TIMESTAMP,
    last_modified TIMESTAMP,
    FOREIGN KEY(dir+id) REFERENCES Directories(dir_id),
    FOREIGN KEY(owner_id) REFERENCES Users(user_id)
)

Create Table Shared_Files (
    file_id INT NOT NULL,
    owner_id INT NOT NULL,
    shared_user_id INT NOT NULL,
    access_level VARCHAR(63) NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES Users(user_id),
    FOREIGN KEY (shared_user_id) REFERENCES Users(user_id),
    FOREIGN KEY (file_id) REFERENCES Files(file_id),
    PRIMARY KEY (file_id, owner_id, shared_user_id)
)