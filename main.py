import sys
import subprocess
import threading
import time
import json
import sqlite3
import shutil
import signal
import logging
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Third-party imports
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_sock import Sock
import psutil
from pyngrok import ngrok

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'kustify-secret-key-change-in-production'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///kustify.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PROJECTS_ROOT = os.path.join(os.getcwd(), 'users')
    LOG_RETENTION_DAYS = 7
    
    # Docker configuration for different platforms
    if platform.system() == 'Windows':
        DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET') or 'npipe:////./pipe/docker_engine'
    else:
        DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET') or 'unix://var/run/docker.sock'
    
    NGROK_AUTH_TOKEN = os.environ.get('32SRDtFhdpCQc5dRCq3uxt9gKJp_4ZhMRojZbcD1XXXx3VAA6') or None
    
    # Subscription plans with resource limits
    PLANS = {
        'free': {
            'max_projects': 1,
            'cpu_limit': 0.1,  # vCPU
            'memory_limit': 512,  # MB
            'disk_limit': 1024,  # MB
            'max_ngrok_tunnels': 0
        },
        'premium': {
            'max_projects': 10,
            'cpu_limit': 0.5,
            'memory_limit': 2048,
            'disk_limit': 5120,
            'max_ngrok_tunnels': 1
        },
        'enterprise': {
            'max_projects': -1,  # unlimited
            'cpu_limit': 2.0,
            'memory_limit': 8192,
            'disk_limit': 20480,
            'max_ngrok_tunnels': 5
        }
    }

# Initialize Flask app
app = Flask(__name__)
app.config.from_object(Config)
sock = Sock(app)

# Initialize database
db = SQLAlchemy(app)

# Initialize login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize Docker client
docker_client = None
docker_available = False
try:
    import docker
    # Try different base URLs based on platform
    if platform.system() == 'Windows':
        docker_urls = [
            Config.DOCKER_SOCKET,  # Default from config
            'npipe:////./pipe/docker_engine',  # Default Windows named pipe
            'tcp://localhost:2375'  # Docker daemon exposed on TCP
        ]
    else:
        docker_urls = [
            Config.DOCKER_SOCKET,  # Default from config
            'unix://var/run/docker.sock',  # Default Unix socket
            'tcp://localhost:2375'  # Docker daemon exposed on TCP
        ]

    # Try each URL until one works
    for url in docker_urls:
        try:
            docker_client = docker.DockerClient(base_url=url)
            docker_client.ping()  # Test connection
            docker_available = True
            logger.info(f"Docker client initialized successfully using {url}")
            break
        except Exception as url_error:
            logger.debug(f"Failed to connect to Docker at {url}: {str(url_error)}")
            continue
except Exception as e:
    docker_available = False
    logger.error(f"Failed to initialize Docker client: {str(e)}")

# Initialize Ngrok
ngrok_available = False
try:
    if Config.NGROK_AUTH_TOKEN:
        from pyngrok import ngrok, conf
        conf.set_default_auth_token(Config.NGROK_AUTH_TOKEN)
        ngrok_available = True
        logger.info("Ngrok initialized successfully")
    else:
        logger.warning("Ngrok auth token not provided, Ngrok features will be disabled")
except Exception as e:
    logger.error(f"Failed to initialize Ngrok: {str(e)}")

# Ensure projects directory exists
os.makedirs(Config.PROJECTS_ROOT, exist_ok=True)

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    plan = db.Column(db.String(20), default='free')  # free, premium, enterprise
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    projects = db.relationship('Project', backref='user', lazy=True, cascade='all, delete-orphan')
    ngrok_tunnels = db.relationship('NgrokTunnel', backref='user', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<User {self.username}>'
    
    def get_plan_limits(self):
        return Config.PLANS.get(self.plan, Config.PLANS['free'])

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255))
    template = db.Column(db.String(50), nullable=False)  # pyrogram-bot, static-site, web-service, worker, vps, github-docker, github-custom
    status = db.Column(db.String(20), default='stopped')  # stopped, running, deploying, error
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    port = db.Column(db.Integer, default=8000)
    config = db.Column(db.Text)  # JSON string for additional config
    container_id = db.Column(db.String(64))  # Docker container ID
    ngrok_tunnel_id = db.Column(db.Integer, db.ForeignKey('ngrok_tunnel.id'))
    github_repo = db.Column(db.String(255))  # GitHub repository URL

    def __repr__(self):
        return f'<Project {self.name}>'

class NgrokTunnel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'))
    public_url = db.Column(db.String(255))
    local_port = db.Column(db.Integer)
    proto = db.Column(db.String(10), default='http')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    active = db.Column(db.Boolean, default=True)
    
    # Fix the relationship ambiguity by specifying foreign_keys
    project = db.relationship('Project', backref=db.backref('ngrok_tunnel', uselist=False), 
                              foreign_keys=[project_id])

# Function to check and update database schema
def update_database_schema():
    try:
        # Check if the database file exists
        db_path = Config.SQLALCHEMY_DATABASE_URI.replace('sqlite:///', '')
        if os.path.exists(db_path):
            # Connect to the database and check if the container_id column exists
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Check if container_id column exists in project table
            cursor.execute("PRAGMA table_info(project)")
            columns = [column[1] for column in cursor.fetchall()]
            
            # If container_id column doesn't exist, recreate the database
            if 'container_id' not in columns:
                logger.info("Database schema is outdated, recreating database...")
                conn.close()
                os.remove(db_path)
                with app.app_context():
                    db.create_all()
                logger.info("Database recreated with updated schema")
            else:
                conn.close()
    except Exception as e:
        logger.error(f"Error updating database schema: {str(e)}")
        # If there's an error, try to recreate the database
        try:
            db_path = Config.SQLALCHEMY_DATABASE_URI.replace('sqlite:///', '')
            if os.path.exists(db_path):
                os.remove(db_path)
            with app.app_context():
                db.create_all()
            logger.info("Database recreated after error")
        except Exception as e2:
            logger.error(f"Failed to recreate database: {str(e2)}")

# Create tables with schema check
with app.app_context():
    update_database_schema()
    db.create_all()

# Authentication
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def hash_password(password):
    # In a real application, use a proper password hashing library like bcrypt
    return password  # This is just for demonstration, not secure!

def check_password(hashed_password, password):
    return hashed_password == password  # This is just for demonstration, not secure!

# HTML Templates (simplified for brevity, but enhanced for UI)
LANDING_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kustify by KustBots - Premium Bot Hosting Platform</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @keyframes float {
            0% { transform: translateY(0px); }
            50% { transform: translateY(-20px); }
            100% { transform: translateY(0px); }
        }
        .float-animation {
            animation: float 6s ease-in-out infinite;
        }
        @keyframes gradient {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        .gradient-bg {
            background: linear-gradient(-45deg, #ee7752, #e73c7e, #23a6d5, #23d5ab);
            background-size: 400% 400%;
            animation: gradient 15s ease infinite;
        }
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
        .transition-all {
            transition: all 0.3s ease;
        }
    </style>
</head>
<body class="bg-gray-50">
    <!-- Navigation -->
    <nav class="bg-white shadow-lg">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-2xl font-bold gradient-text">Kustify</span>
                        <span class="ml-1 text-gray-600">by KustBots</span>
                    </div>
                    <div class="hidden md:ml-6 md:flex md:space-x-8">
                        <a href="#" class="border-indigo-500 text-gray-900 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Home
                        </a>
                        <a href="#features" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Features
                        </a>
                        <a href="#pricing" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Pricing
                        </a>
                        <a href="#about" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            About
                        </a>
                    </div>
                </div>
                <div class="flex items-center">
                    <div class="flex-shrink-0">
                        <a href="/login" class="relative inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 shadow-sm hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-all">
                            Sign in
                        </a>
                        <a href="/signup" class="ml-3 relative inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-all">
                            Sign up
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Hero Section -->
    <div class="gradient-bg">
        <div class="max-w-7xl mx-auto py-16 px-4 sm:px-6 lg:px-8">
            <div class="text-center">
                <h1 class="text-4xl tracking-tight font-extrabold text-white sm:text-5xl md:text-6xl">
                    <span class="block">Premium Bot Hosting</span>
                    <span class="block text-indigo-200">Made Simple</span>
                </h1>
                <p class="mt-3 max-w-md mx-auto text-base text-indigo-100 sm:text-lg md:mt-5 md:text-xl md:max-w-3xl">
                    Deploy and manage your Telegram bots, websites, and applications with ease. Kustify by KustBots provides a powerful yet simple platform for all your hosting needs.
                </p>
                <div class="mt-5 max-w-md mx-auto sm:flex sm:justify-center md:mt-8">
                    <div class="rounded-md shadow transform hover:scale-105 transition-all">
                        <a href="/signup" class="w-full flex items-center justify-center px-8 py-3 border border-transparent text-base font-medium rounded-md text-indigo-600 bg-white hover:bg-gray-50 md:py-4 md:text-lg md:px-10">
                            Get started
                        </a>
                    </div>
                    <div class="mt-3 rounded-md shadow sm:mt-0 sm:ml-3 transform hover:scale-105 transition-all">
                        <a href="#features" class="w-full flex items-center justify-center px-8 py-3 border border-transparent text-base font-medium rounded-md text-white bg-indigo-600 bg-opacity-60 hover:bg-opacity-70 md:py-4 md:text-lg md:px-10">
                            Live demo
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Features Section -->
    <div id="features" class="py-12 bg-white">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="text-center">
                <h2 class="text-base text-indigo-600 font-semibold tracking-wide uppercase">Features</h2>
                <p class="mt-2 text-3xl leading-8 font-extrabold tracking-tight text-gray-900 sm:text-4xl">
                    Everything you need to host your projects
                </p>
                <p class="mt-4 max-w-2xl text-xl text-gray-500 lg:mx-auto">
                    Kustify by KustBots provides all the tools you need to deploy and manage your applications with ease.
                </p>
            </div>

            <div class="mt-10">
                <div class="space-y-10 md:space-y-0 md:grid md:grid-cols-2 md:gap-x-8 md:gap-y-10">
                    <!-- Feature 1 -->
                    <div class="relative">
                        <div class="absolute flex items-center justify-center h-12 w-12 rounded-md bg-indigo-500 text-white">
                            <i class="fas fa-rocket"></i>
                        </div>
                        <p class="ml-16 text-lg leading-6 font-medium text-gray-900">Easy Deployment</p>
                        <p class="mt-2 ml-16 text-base text-gray-500">
                            Deploy your projects with just a few clicks. No complex configuration required.
                        </p>
                    </div>

                    <!-- Feature 2 -->
                    <div class="relative">
                        <div class="absolute flex items-center justify-center h-12 w-12 rounded-md bg-indigo-500 text-white">
                            <i class="fas fa-microchip"></i>
                        </div>
                        <p class="ml-16 text-lg leading-6 font-medium text-gray-900">Resource Management</p>
                        <p class="mt-2 ml-16 text-base text-gray-500">
                            Control CPU and memory usage for each deployment with advanced resource limiting.
                        </p>
                    </div>

                    <!-- Feature 3 -->
                    <div class="relative">
                        <div class="absolute flex items-center justify-center h-12 w-12 rounded-md bg-indigo-500 text-white">
                            <i class="fas fa-terminal"></i>
                        </div>
                        <p class="ml-16 text-lg leading-6 font-medium text-gray-900">Web-Based Terminal</p>
                        <p class="mt-2 ml-16 text-base text-gray-500">
                            Access your VPS containers directly from your browser with our web-based terminal.
                        </p>
                    </div>

                    <!-- Feature 4 -->
                    <div class="relative">
                        <div class="absolute flex items-center justify-center h-12 w-12 rounded-md bg-indigo-500 text-white">
                            <i class="fas fa-shield-alt"></i>
                        </div>
                        <p class="ml-16 text-lg leading-6 font-medium text-gray-900">Secure & Reliable</p>
                        <p class="mt-2 ml-16 text-base text-gray-500">
                            Your projects are isolated and secure with our advanced security measures.
                        </p>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Footer -->
    <footer class="bg-gray-800 text-white py-8">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex flex-col md:flex-row justify-between items-center">
                <div class="mb-4 md:mb-0">
                    <span class="text-xl font-bold gradient-text">Kustify</span>
                    <span class="ml-1 text-gray-400">by KustBots</span>
                </div>
                <div class="flex space-x-6">
                    <a href="#" class="text-gray-400 hover:text-white transition-all">
                        <i class="fab fa-github"></i>
                    </a>
                    <a href="#" class="text-gray-400 hover:text-white transition-all">
                        <i class="fab fa-twitter"></i>
                    </a>
                    <a href="#" class="text-gray-400 hover:text-white transition-all">
                        <i class="fab fa-telegram"></i>
                    </a>
                </div>
            </div>
            <div class="mt-8 text-center text-gray-400 text-sm">
                &copy; 2023 Kustify by KustBots. All rights reserved.
            </div>
        </div>
    </footer>
</body>
</html>
"""

LOGIN_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Kustify by KustBots</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .gradient-bg {
            background: linear-gradient(-45deg, #ee7752, #e73c7e, #23a6d5, #23d5ab);
            background-size: 400% 400%;
            animation: gradient 15s ease infinite;
        }
        @keyframes gradient {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
    </style>
</head>
<body class="bg-gray-50">
    <div class="min-h-screen flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
        <div class="max-w-md w-full space-y-8">
            <div>
                <div class="mx-auto h-12 w-auto flex justify-center">
                    <span class="text-3xl font-bold gradient-text">Kustify</span>
                </div>
                <h2 class="mt-6 text-center text-3xl font-extrabold text-gray-900">
                    Sign in to your account
                </h2>
                <p class="mt-2 text-center text-sm text-gray-600">
                    Or
                    <a href="/signup" class="font-medium text-indigo-600 hover:text-indigo-500">
                        create a new account
                    </a>
                </p>
            </div>
            <form class="mt-8 space-y-6" action="/login" method="POST">
                {% if error %}
                <div class="rounded-md bg-red-50 p-4">
                    <div class="flex">
                        <div class="flex-shrink-0">
                            <svg class="h-5 w-5 text-red-400" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
                            </svg>
                        </div>
                        <div class="ml-3">
                            <h3 class="text-sm font-medium text-red-800">
                                {{ error }}
                            </h3>
                        </div>
                    </div>
                </div>
                {% endif %}
                <div class="rounded-md shadow-sm -space-y-px">
                    <div>
                        <label for="username" class="sr-only">Username</label>
                        <input id="username" name="username" type="text" required class="appearance-none rounded-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-t-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Username">
                    </div>
                    <div>
                        <label for="password" class="sr-only">Password</label>
                        <input id="password" name="password" type="password" required class="appearance-none rounded-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-b-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Password">
                    </div>
                </div>

                <div class="flex items-center justify-between">
                    <div class="flex items-center">
                        <input id="remember-me" name="remember-me" type="checkbox" class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded">
                        <label for="remember-me" class="ml-2 block text-sm text-gray-900">
                            Remember me
                        </label>
                    </div>

                    <div class="text-sm">
                        <a href="#" class="font-medium text-indigo-600 hover:text-indigo-500">
                            Forgot your password?
                        </a>
                    </div>
                </div>

                <div>
                    <button type="submit" class="group relative w-full flex justify-center py-2 px-4 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                        <span class="absolute left-0 inset-y-0 flex items-center pl-3">
                            <svg class="h-5 w-5 text-indigo-500 group-hover:text-indigo-400" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                                <path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
                            </svg>
                        </span>
                        Sign in
                    </button>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
"""

SIGNUP_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign Up - Kustify by KustBots</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .gradient-bg {
            background: linear-gradient(-45deg, #ee7752, #e73c7e, #23a6d5, #23d5ab);
            background-size: 400% 400%;
            animation: gradient 15s ease infinite;
        }
        @keyframes gradient {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
    </style>
</head>
<body class="bg-gray-50">
    <div class="min-h-screen flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
        <div class="max-w-md w-full space-y-8">
            <div>
                <div class="mx-auto h-12 w-auto flex justify-center">
                    <span class="text-3xl font-bold gradient-text">Kustify</span>
                </div>
                <h2 class="mt-6 text-center text-3xl font-extrabold text-gray-900">
                    Create your account
                </h2>
                <p class="mt-2 text-center text-sm text-gray-600">
                    Or
                    <a href="/login" class="font-medium text-indigo-600 hover:text-indigo-500">
                        sign in to your existing account
                    </a>
                </p>
            </div>
            <form class="mt-8 space-y-6" action="/signup" method="POST">
                {% if error %}
                <div class="rounded-md bg-red-50 p-4">
                    <div class="flex">
                        <div class="flex-shrink-0">
                            <svg class="h-5 w-5 text-red-400" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
                            </svg>
                        </div>
                        <div class="ml-3">
                            <h3 class="text-sm font-medium text-red-800">
                                {{ error }}
                            </h3>
                        </div>
                    </div>
                </div>
                {% endif %}
                <div class="space-y-4">
                    <div>
                        <label for="username" class="block text-sm font-medium text-gray-700">Username</label>
                        <div class="mt-1">
                            <input id="username" name="username" type="text" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Username">
                        </div>
                    </div>
                    <div>
                        <label for="email" class="block text-sm font-medium text-gray-700">Email</label>
                        <div class="mt-1">
                            <input id="email" name="email" type="email" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Email">
                        </div>
                    </div>
                    <div>
                        <label for="password" class="block text-sm font-medium text-gray-700">Password</label>
                        <div class="mt-1">
                            <input id="password" name="password" type="password" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Password">
                        </div>
                    </div>
                    <div>
                        <label for="confirm_password" class="block text-sm font-medium text-gray-700">Confirm Password</label>
                        <div class="mt-1">
                            <input id="confirm_password" name="confirm_password" type="password" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Confirm Password">
                        </div>
                    </div>
                </div>

                <div>
                    <button type="submit" class="group relative w-full flex justify-center py-2 px-4 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                        <span class="absolute left-0 inset-y-0 flex items-center pl-3">
                            <svg class="h-5 w-5 text-indigo-500 group-hover:text-indigo-400" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                            </svg>
                        </span>
                        Sign up
                    </button>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
"""

DASHBOARD_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - Kustify by KustBots</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .fade-in {
            animation: fadeIn 0.5s ease-out forwards;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        .pulse {
            animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
        }
        .project-card {
            transition: all 0.3s ease;
        }
        .project-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
        }
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
    </style>
</head>
<body class="bg-gray-50">
    <!-- Navigation -->
    <nav class="bg-white shadow-lg">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-2xl font-bold gradient-text">Kustify</span>
                        <span class="ml-1 text-gray-600">by KustBots</span>
                    </div>
                    <div class="hidden md:ml-6 md:flex md:space-x-8">
                        <a href="/dashboard" class="border-indigo-500 text-gray-900 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Dashboard
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Projects
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Settings
                        </a>
                    </div>
                </div>
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-gray-700 mr-3">Welcome, {{ current_user.username }}</span>
                        <span class="text-sm bg-indigo-100 text-indigo-800 px-2 py-1 rounded-full">{{ current_user.plan }}</span>
                        <a href="/logout" class="ml-4 inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-all">
                            <i class="fas fa-sign-out-alt mr-1"></i> Logout
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div class="px-4 py-6 sm:px-0">
            <div class="flex justify-between items-center mb-6">
                <h1 class="text-2xl font-bold text-gray-900">Your Projects</h1>
                <a href="/new-deployment" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                    <i class="fas fa-plus mr-2"></i> Create New Project
                </a>
            </div>

            {% if not docker_available %}
            <div class="rounded-md bg-yellow-50 p-4 mb-6">
                <div class="flex">
                    <div class="flex-shrink-0">
                        <svg class="h-5 w-5 text-yellow-400" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                            <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
                        </svg>
                    </div>
                    <div class="ml-3">
                        <h3 class="text-sm font-medium text-yellow-800">
                            Docker not available
                        </h3>
                        <div class="mt-2 text-sm text-yellow-700">
                            <p>
                                Docker is not running or not installed. Some features may not work properly.
                                Please install and start Docker to use all features.
                            </p>
                        </div>
                    </div>
                </div>
            </div>
            {% endif %}

            {% if projects %}
            <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {% for project in projects %}
                <div class="project-card bg-white overflow-hidden shadow rounded-lg fade-in" style="animation-delay: {{ loop.index * 0.1 }}s;">
                    <div class="px-4 py-5 sm:p-6">
                        <div class="flex justify-between items-start">
                            <div>
                                <h3 class="text-lg leading-6 font-medium text-gray-900">{{ project.name }}</h3>
                                <p class="mt-1 max-w-2xl text-sm text-gray-500">{{ project.description or 'No description' }}</p>
                            </div>
                            <div class="ml-2 flex-shrink-0 flex">
                                {% if project.status == 'running' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">
                                    Running
                                </span>
                                {% elif project.status == 'deploying' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800 pulse">
                                    Deploying <i class="fas fa-spinner fa-spin ml-1"></i>
                                </span>
                                {% elif project.status == 'stopped' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">
                                    Stopped
                                </span>
                                {% elif project.status == 'error' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800">
                                    Error
                                </span>
                                {% endif %}
                            </div>
                        </div>
                        
                        <div class="mt-4 flex items-center text-sm text-gray-500">
                            <i class="fas fa-folder mr-1.5"></i>
                            <span class="capitalize">{{ project.template.replace('-', ' ') }}</span>
                            {% if project.github_repo %}
                            <span class="ml-2"><i class="fab fa-github mr-1"></i> GitHub</span>
                            {% endif %}
                        </div>
                        
                        <div class="mt-6 flex justify-between">
                            <div class="flex space-x-2">
                                <a href="/project/{{ project.id }}/logs" class="inline-flex items-center px-3 py-1 border border-gray-300 shadow-sm text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                                    <i class="fas fa-file-alt mr-1"></i> Logs
                                </a>
                                
                                {% if project.template == 'vps' %}
                                <a href="/project/{{ project.id }}/terminal" class="inline-flex items-center px-3 py-1 border border-gray-300 shadow-sm text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                                    <i class="fas fa-terminal mr-1"></i> Terminal
                                </a>
                                {% endif %}
                                
                                {% if project.ngrok_tunnel %}
                                <a href="{{ project.ngrok_tunnel.public_url }}" target="_blank" class="inline-flex items-center px-3 py-1 border border-gray-300 shadow-sm text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200">
                                    <i class="fas fa-external-link-alt mr-1"></i> Open
                                </a>
                                {% endif %}
                            </div>
                            
                            <div class="flex space-x-2">
                                {% if project.status == 'running' %}
                                <form action="/project/{{ project.id }}/stop" method="post" class="inline">
                                    <button type="submit" class="inline-flex items-center px-3 py-1 border border-transparent text-sm font-medium rounded-md text-white bg-red-600 hover:bg-red-700">
                                        <i class="fas fa-stop mr-1"></i> Stop
                                    </button>
                                </form>
                                {% elif project.status == 'stopped' or project.status == 'error' %}
                                <form action="/project/{{ project.id }}/start" method="post" class="inline">
                                    <button type="submit" class="inline-flex items-center px-3 py-1 border border-transparent text-sm font-medium rounded-md text-white bg-green-600 hover:bg-green-700">
                                        <i class="fas fa-play mr-1"></i> Start
                                    </button>
                                </form>
                                {% endif %}
                                
                                <form action="/project/{{ project.id }}/delete" method="post" class="inline" onsubmit="return confirm('Are you sure you want to delete this project?');">
                                    <button type="submit" class="inline-flex items-center px-3 py-1 border border-transparent text-sm font-medium rounded-md text-white bg-red-600 hover:bg-red-700">
                                        <i class="fas fa-trash mr-1"></i> Delete
                                    </button>
                                </form>
                            </div>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <div class="text-center py-12">
                <svg class="mx-auto h-12 w-12 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
                </svg>
                <h3 class="mt-2 text-sm font-medium text-gray-900">No projects</h3>
                <p class="mt-1 text-sm text-gray-500">Get started by creating a new project.</p>
                <div class="mt-6">
                    <a href="/new-deployment" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                        <i class="fas fa-plus mr-2"></i> Create New Project
                    </a>
                </div>
            </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

NEW_DEPLOYMENT_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>New Deployment - Kustify by KustBots</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .wizard-step {
            display: none;
        }
        .wizard-step.active {
            display: block;
        }
        .progress-step {
            transition: all 0.3s ease;
        }
        .progress-step.active {
            background-color: #4f46e5;
            color: white;
        }
        .progress-step.completed {
            background-color: #10b981;
            color: white;
        }
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
        .project-type-option {
            transition: all 0.3s ease;
        }
        .project-type-option:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
        }
        .project-type-option.selected {
            border-color: #4f46e5;
            background-color: #f0f9ff;
        }
    </style>
</head>
<body class="bg-gray-50">
    <!-- Navigation -->
    <nav class="bg-white shadow-lg">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-2xl font-bold gradient-text">Kustify</span>
                        <span class="ml-1 text-gray-600">by KustBots</span>
                    </div>
                    <div class="hidden md:ml-6 md:flex md:space-x-8">
                        <a href="/dashboard" class="border-indigo-500 text-gray-900 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Dashboard
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Projects
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Settings
                        </a>
                    </div>
                </div>
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-gray-700 mr-3">Welcome, {{ current_user.username }}</span>
                        <span class="text-sm bg-indigo-100 text-indigo-800 px-2 py-1 rounded-full">{{ current_user.plan }}</span>
                        <a href="/logout" class="ml-4 inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-all">
                            <i class="fas fa-sign-out-alt mr-1"></i> Logout
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div class="px-4 py-6 sm:px-0">
            <div class="flex justify-between items-center mb-6">
                <h1 class="text-2xl font-bold text-gray-900">Create New Project</h1>
                <a href="/dashboard" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                    <i class="fas fa-arrow-left mr-2"></i> Back to Dashboard
                </a>
            </div>

            {% if error %}
            <div class="rounded-md bg-red-50 p-4 mb-6">
                <div class="flex">
                    <div class="flex-shrink-0">
                        <svg class="h-5 w-5 text-red-400" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                            <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
                        </svg>
                    </div>
                    <div class="ml-3">
                        <h3 class="text-sm font-medium text-red-800">
                            {{ error }}
                        </h3>
                    </div>
                </div>
            </div>
            {% endif %}

            <!-- Progress Steps -->
            <div class="mb-8">
                <div class="flex items-center justify-between">
                    <div class="flex items-center">
                        <div class="progress-step active rounded-full h-10 w-10 flex items-center justify-center bg-indigo-600 text-white font-medium" id="step1-indicator">
                            1
                        </div>
                        <div class="ml-4">
                            <h3 class="text-sm font-medium text-gray-900">Project Type</h3>
                        </div>
                    </div>
                    <div class="flex-1 h-1 mx-4 bg-gray-200"></div>
                    <div class="flex items-center">
                        <div class="progress-step rounded-full h-10 w-10 flex items-center justify-center bg-gray-200 text-gray-600 font-medium" id="step2-indicator">
                            2
                        </div>
                        <div class="ml-4">
                            <h3 class="text-sm font-medium text-gray-500">Configuration</h3>
                        </div>
                    </div>
                    <div class="flex-1 h-1 mx-4 bg-gray-200"></div>
                    <div class="flex items-center">
                        <div class="progress-step rounded-full h-10 w-10 flex items-center justify-center bg-gray-200 text-gray-600 font-medium" id="step3-indicator">
                            3
                        </div>
                        <div class="ml-4">
                            <h3 class="text-sm font-medium text-gray-500">Summary</h3>
                        </div>
                    </div>
                </div>
            </div>

            <form action="/new-deployment" method="POST" id="deployment-form">
                <!-- Step 1: Project Type Selection -->
                <div class="wizard-step active" id="step1">
                    <h4 class="text-md font-medium text-gray-900 mb-4">Choose Project Type</h4>
                    
                    <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                        <div class="project-type-option border rounded-lg p-4 cursor-pointer hover:border-indigo-500" data-type="web-service">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 bg-indigo-100 rounded-md p-3">
                                    <i class="fas fa-globe text-indigo-600"></i>
                                </div>
                                <div class="ml-4">
                                    <h5 class="text-lg font-medium text-gray-900">Web Service</h5>
                                    <p class="text-sm text-gray-500">Deploy a web application or API</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="project-type-option border rounded-lg p-4 cursor-pointer hover:border-indigo-500" data-type="static-site">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 bg-indigo-100 rounded-md p-3">
                                    <i class="fas fa-file-code text-indigo-600"></i>
                                </div>
                                <div class="ml-4">
                                    <h5 class="text-lg font-medium text-gray-900">Static Site</h5>
                                    <p class="text-sm text-gray-500">Deploy a static HTML website</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="project-type-option border rounded-lg p-4 cursor-pointer hover:border-indigo-500" data-type="vps">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 bg-indigo-100 rounded-md p-3">
                                    <i class="fas fa-server text-indigo-600"></i>
                                </div>
                                <div class="ml-4">
                                    <h5 class="text-lg font-medium text-gray-900">VPS</h5>
                                    <p class="text-sm text-gray-500">Deploy a virtual private server</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="project-type-option border rounded-lg p-4 cursor-pointer hover:border-indigo-500" data-type="pyrogram-bot">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 bg-indigo-100 rounded-md p-3">
                                    <i class="fas fa-robot text-indigo-600"></i>
                                </div>
                                <div class="ml-4">
                                    <h5 class="text-lg font-medium text-gray-900">Telegram Bot</h5>
                                    <p class="text-sm text-gray-500">Deploy a Telegram bot using Pyrogram</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="project-type-option border rounded-lg p-4 cursor-pointer hover:border-indigo-500" data-type="github-docker">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 bg-indigo-100 rounded-md p-3">
                                    <i class="fab fa-github text-indigo-600"></i>
                                </div>
                                <div class="ml-4">
                                    <h5 class="text-lg font-medium text-gray-900">GitHub Repo (Docker)</h5>
                                    <p class="text-sm text-gray-500">Deploy a GitHub repository with Dockerfile</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="project-type-option border rounded-lg p-4 cursor-pointer hover:border-indigo-500" data-type="github-custom">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 bg-indigo-100 rounded-md p-3">
                                    <i class="fab fa-github text-indigo-600"></i>
                                </div>
                                <div class="ml-4">
                                    <h5 class="text-lg font-medium text-gray-900">GitHub Repo (Custom)</h5>
                                    <p class="text-sm text-gray-500">Deploy a GitHub repository with custom commands</p>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="mt-8 flex justify-end">
                        <button type="button" id="next-step1" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50" disabled>
                            Next <i class="fas fa-arrow-right ml-2"></i>
                        </button>
                    </div>
                </div>
                
                <!-- Step 2: Configuration -->
                <div class="wizard-step" id="step2">
                    <h4 class="text-md font-medium text-gray-900 mb-4">Project Configuration</h4>
                    
                    <div class="space-y-6">
                        <div>
                            <label for="name" class="block text-sm font-medium text-gray-700">Project Name</label>
                            <div class="mt-1">
                                <input type="text" name="name" id="name" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="My Awesome Project">
                            </div>
                        </div>
                        
                        <div>
                            <label for="description" class="block text-sm font-medium text-gray-700">Description</label>
                            <div class="mt-1">
                                <textarea name="description" id="description" rows="3" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="A brief description of your project"></textarea>
                            </div>
                        </div>
                        
                        <!-- Configuration fields based on project type -->
                        <div id="web-service-config" class="config-section hidden">
                            <div>
                                <label for="requirements" class="block text-sm font-medium text-gray-700">Requirements (one per line)</label>
                                <div class="mt-1">
                                    <textarea name="requirements" id="requirements" rows="4" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="flask&#10;requests&#10;gunicorn">flask&#10;requests&#10;gunicorn</textarea>
                                </div>
                            </div>
                            
                            <div>
                                <label for="main_file" class="block text-sm font-medium text-gray-700">Main File</label>
                                <div class="mt-1">
                                    <input type="text" name="main_file" id="main_file" value="app.py" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm">
                                </div>
                            </div>
                            
                            <div>
                                <label for="port" class="block text-sm font-medium text-gray-700">Port</label>
                                <div class="mt-1">
                                    <input type="number" name="port" id="port" value="8000" min="1000" max="65535" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm">
                                </div>
                            </div>
                            
                            <div>
                                <label for="ngrok_token" class="block text-sm font-medium text-gray-700">Ngrok Auth Token (Optional)</label>
                                <div class="mt-1">
                                    <input type="text" name="ngrok_token" id="ngrok_token" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Your Ngrok auth token">
                                </div>
                                <p class="mt-2 text-sm text-gray-500">If provided, your web service will be accessible via a public URL</p>
                            </div>
                        </div>
                        
                        <div id="static-site-config" class="config-section hidden">
                            <div>
                                <label for="index_html" class="block text-sm font-medium text-gray-700">Index HTML</label>
                                <div class="mt-1">
                                    <textarea name="index_html" id="index_html" rows="10" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="&lt;!DOCTYPE html&gt;&#10;&lt;html&gt;&#10;  &lt;head&gt;&#10;    &lt;title&gt;My Site&lt;/title&gt;&#10;  &lt;/head&gt;&#10;  &lt;body&gt;&#10;    &lt;h1&gt;Hello World!&lt;/h1&gt;&#10;  &lt;/body&gt;&#10;&lt;/html&gt;">&lt;!DOCTYPE html&gt;
&lt;html&gt;
  &lt;head&gt;
    &lt;title&gt;My Site&lt;/title&gt;
  &lt;/head&gt;
  &lt;body&gt;
    &lt;h1&gt;Hello World!&lt;/h1&gt;
  &lt;/body&gt;
&lt;/html&gt;</textarea>
                                </div>
                            </div>
                            
                            <div>
                                <label for="port" class="block text-sm font-medium text-gray-700">Port</label>
                                <div class="mt-1">
                                    <input type="number" name="port" id="port" value="8000" min="1000" max="65535" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm">
                                </div>
                            </div>
                        </div>
                        
                        <div id="vps-config" class="config-section hidden">
                            <div>
                                <label for="os" class="block text-sm font-medium text-gray-700">Operating System</label>
                                <div class="mt-1">
                                    <select name="os" id="os" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm">
                                        <option value="ubuntu:22.04">Ubuntu 22.04</option>
                                        <option value="ubuntu:20.04">Ubuntu 20.04</option>
                                        <option value="debian:11">Debian 11</option>
                                        <option value="centos:8">CentOS 8</option>
                                    </select>
                                </div>
                            </div>
                            
                            <div>
                                <label for="packages" class="block text-sm font-medium text-gray-700">Additional Packages (space separated)</label>
                                <div class="mt-1">
                                    <input type="text" name="packages" id="packages" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="curl wget git vim htop">
                                </div>
                            </div>
                        </div>
                        
                        <div id="pyrogram-bot-config" class="config-section hidden">
                            <div>
                                <label for="bot_token" class="block text-sm font-medium text-gray-700">Bot Token</label>
                                <div class="mt-1">
                                    <input type="text" name="bot_token" id="bot_token" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz">
                                </div>
                            </div>
                            
                            <div>
                                <label for="api_id" class="block text-sm font-medium text-gray-700">API ID</label>
                                <div class="mt-1">
                                    <input type="number" name="api_id" id="api_id" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="1234567">
                                </div>
                            </div>
                            
                            <div>
                                <label for="api_hash" class="block text-sm font-medium text-gray-700">API Hash</label>
                                <div class="mt-1">
                                    <input type="text" name="api_hash" id="api_hash" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="abcdef1234567890abcdef1234567890">
                                </div>
                            </div>
                        </div>
                        
                        <div id="github-docker-config" class="config-section hidden">
                            <div>
                                <label for="github_repo" class="block text-sm font-medium text-gray-700">GitHub Repository URL</label>
                                <div class="mt-1">
                                    <input type="text" name="github_repo" id="github_repo" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="https://github.com/username/repo">
                                </div>
                            </div>
                            
                            <div>
                                <label for="port" class="block text-sm font-medium text-gray-700">Port</label>
                                <div class="mt-1">
                                    <input type="number" name="port" id="port" value="8000" min="1000" max="65535" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm">
                                </div>
                            </div>
                            
                            <div>
                                <label for="ngrok_token" class="block text-sm font-medium text-gray-700">Ngrok Auth Token (Optional)</label>
                                <div class="mt-1">
                                    <input type="text" name="ngrok_token" id="ngrok_token" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Your Ngrok auth token">
                                </div>
                                <p class="mt-2 text-sm text-gray-500">If provided, your web service will be accessible via a public URL</p>
                            </div>
                        </div>
                        
                        <div id="github-custom-config" class="config-section hidden">
                            <div>
                                <label for="github_repo" class="block text-sm font-medium text-gray-700">GitHub Repository URL</label>
                                <div class="mt-1">
                                    <input type="text" name="github_repo" id="github_repo" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="https://github.com/username/repo">
                                </div>
                            </div>
                            
                            <div>
                                <label for="build_command" class="block text-sm font-medium text-gray-700">Build Command</label>
                                <div class="mt-1">
                                    <input type="text" name="build_command" id="build_command" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="npm install">
                                </div>
                                <p class="mt-2 text-sm text-gray-500">Leave empty if no build step is needed</p>
                            </div>
                            
                            <div>
                                <label for="start_command" class="block text-sm font-medium text-gray-700">Start Command</label>
                                <div class="mt-1">
                                    <input type="text" name="start_command" id="start_command" required class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="python app.py">
                                </div>
                            </div>
                            
                            <div>
                                <label for="port" class="block text-sm font-medium text-gray-700">Port</label>
                                <div class="mt-1">
                                    <input type="number" name="port" id="port" value="8000" min="1000" max="65535" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm">
                                </div>
                            </div>
                            
                            <div>
                                <label for="ngrok_token" class="block text-sm font-medium text-gray-700">Ngrok Auth Token (Optional)</label>
                                <div class="mt-1">
                                    <input type="text" name="ngrok_token" id="ngrok_token" class="appearance-none relative block w-full px-3 py-2 border border-gray-300 placeholder-gray-500 text-gray-900 rounded-md focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 focus:z-10 sm:text-sm" placeholder="Your Ngrok auth token">
                                </div>
                                <p class="mt-2 text-sm text-gray-500">If provided, your web service will be accessible via a public URL</p>
                            </div>
                        </div>
                    </div>
                    
                    <div class="mt-8 flex justify-between">
                        <button type="button" id="prev-step2" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                            <i class="fas fa-arrow-left mr-2"></i> Previous
                        </button>
                        <button type="button" id="next-step2" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                            Next <i class="fas fa-arrow-right ml-2"></i>
                        </button>
                    </div>
                </div>
                
                <!-- Step 3: Summary -->
                <div class="wizard-step" id="step3">
                    <h4 class="text-md font-medium text-gray-900 mb-4">Deployment Summary</h4>
                    
                    <div class="bg-white shadow overflow-hidden sm:rounded-lg">
                        <div class="px-4 py-5 sm:px-6">
                            <h3 class="text-lg leading-6 font-medium text-gray-900" id="summary-name">Project Name</h3>
                            <p class="mt-1 max-w-2xl text-sm text-gray-500" id="summary-description">Project Description</p>
                        </div>
                        <div class="border-t border-gray-200">
                            <dl>
                                <div class="bg-gray-50 px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                                    <dt class="text-sm font-medium text-gray-500">Type</dt>
                                    <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2" id="summary-type">Project Type</dd>
                                </div>
                                <div class="bg-white px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                                    <dt class="text-sm font-medium text-gray-500">Configuration</dt>
                                    <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2" id="summary-config">Configuration Details</dd>
                                </div>
                                <div class="bg-gray-50 px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                                    <dt class="text-sm font-medium text-gray-500">Port</dt>
                                    <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2" id="summary-port">8000</dd>
                                </div>
                                <div class="bg-white px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                                    <dt class="text-sm font-medium text-gray-500">Ngrok</dt>
                                    <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2" id="summary-ngrok">Not configured</dd>
                                </div>
                            </dl>
                        </div>
                    </div>
                    
                    <div class="mt-8 flex justify-between">
                        <button type="button" id="prev-step3" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                            <i class="fas fa-arrow-left mr-2"></i> Previous
                        </button>
                        <button type="submit" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                            <i class="fas fa-rocket mr-2"></i> Deploy Project
                        </button>
                    </div>
                </div>
            </form>
        </div>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            // Project type selection
            const projectTypeOptions = document.querySelectorAll('.project-type-option');
            const nextStep1Button = document.getElementById('next-step1');
            const prevStep2Button = document.getElementById('prev-step2');
            const nextStep2Button = document.getElementById('next-step2');
            const prevStep3Button = document.getElementById('prev-step3');
            
            let selectedProjectType = '';
            
            projectTypeOptions.forEach(option => {
                option.addEventListener('click', function() {
                    projectTypeOptions.forEach(opt => opt.classList.remove('selected'));
                    this.classList.add('selected');
                    selectedProjectType = this.getAttribute('data-type');
                    nextStep1Button.disabled = false;
                });
            });
            
            // Wizard navigation
            nextStep1Button.addEventListener('click', function() {
                if (selectedProjectType) {
                    // Show the appropriate configuration section
                    document.querySelectorAll('.config-section').forEach(section => {
                        section.classList.add('hidden');
                    });
                    
                    if (selectedProjectType === 'web-service') {
                        document.getElementById('web-service-config').classList.remove('hidden');
                    } else if (selectedProjectType === 'static-site') {
                        document.getElementById('static-site-config').classList.remove('hidden');
                    } else if (selectedProjectType === 'vps') {
                        document.getElementById('vps-config').classList.remove('hidden');
                    } else if (selectedProjectType === 'pyrogram-bot') {
                        document.getElementById('pyrogram-bot-config').classList.remove('hidden');
                    } else if (selectedProjectType === 'github-docker') {
                        document.getElementById('github-docker-config').classList.remove('hidden');
                    } else if (selectedProjectType === 'github-custom') {
                        document.getElementById('github-custom-config').classList.remove('hidden');
                    }
                    
                    // Update progress indicators
                    document.getElementById('step1-indicator').classList.add('completed');
                    document.getElementById('step1-indicator').classList.remove('active');
                    document.getElementById('step2-indicator').classList.add('active');
                    
                    // Show step 2
                    document.getElementById('step1').classList.remove('active');
                    document.getElementById('step2').classList.add('active');
                }
            });
            
            prevStep2Button.addEventListener('click', function() {
                // Update progress indicators
                document.getElementById('step1-indicator').classList.add('active');
                document.getElementById('step1-indicator').classList.remove('completed');
                document.getElementById('step2-indicator').classList.remove('active');
                
                // Show step 1
                document.getElementById('step2').classList.remove('active');
                document.getElementById('step1').classList.add('active');
            });
            
            nextStep2Button.addEventListener('click', function() {
                // Update summary
                const name = document.getElementById('name').value;
                const description = document.getElementById('description').value || 'No description';
                const port = document.getElementById('port') ? document.getElementById('port').value : '8000';
                const ngrokToken = document.getElementById('ngrok_token') ? document.getElementById('ngrok_token').value : '';
                
                document.getElementById('summary-name').textContent = name;
                document.getElementById('summary-description').textContent = description;
                document.getElementById('summary-type').textContent = selectedProjectType.replace('-', ' ').replace(/\b\w/g, l => l.toUpperCase());
                document.getElementById('summary-port').textContent = port;
                document.getElementById('summary-ngrok').textContent = ngrokToken ? 'Configured' : 'Not configured';
                
                // Configuration details based on project type
                let configDetails = '';
                if (selectedProjectType === 'web-service') {
                    const requirements = document.getElementById('requirements').value;
                    const mainFile = document.getElementById('main_file').value;
                    configDetails = `Requirements: ${requirements.replace(/\n/g, ', ')}<br>Main File: ${mainFile}`;
                } else if (selectedProjectType === 'static-site') {
                    configDetails = 'Static HTML site';
                } else if (selectedProjectType === 'vps') {
                    const os = document.getElementById('os').value;
                    const packages = document.getElementById('packages').value || 'None';
                    configDetails = `OS: ${os}<br>Packages: ${packages}`;
                } else if (selectedProjectType === 'pyrogram-bot') {
                    configDetails = 'Pyrogram Telegram Bot';
                } else if (selectedProjectType === 'github-docker') {
                    const githubRepo = document.getElementById('github_repo').value;
                    configDetails = `GitHub Repo: ${githubRepo}<br>Build with Dockerfile`;
                } else if (selectedProjectType === 'github-custom') {
                    const githubRepo = document.getElementById('github_repo').value;
                    const buildCommand = document.getElementById('build_command').value || 'None';
                    const startCommand = document.getElementById('start_command').value;
                    configDetails = `GitHub Repo: ${githubRepo}<br>Build Command: ${buildCommand}<br>Start Command: ${startCommand}`;
                }
                document.getElementById('summary-config').innerHTML = configDetails;
                
                // Update progress indicators
                document.getElementById('step2-indicator').classList.add('completed');
                document.getElementById('step2-indicator').classList.remove('active');
                document.getElementById('step3-indicator').classList.add('active');
                
                // Show step 3
                document.getElementById('step2').classList.remove('active');
                document.getElementById('step3').classList.add('active');
            });
            
            prevStep3Button.addEventListener('click', function() {
                // Update progress indicators
                document.getElementById('step2-indicator').classList.add('active');
                document.getElementById('step2-indicator').classList.remove('completed');
                document.getElementById('step3-indicator').classList.remove('active');
                
                // Show step 2
                document.getElementById('step3').classList.remove('active');
                document.getElementById('step2').classList.add('active');
            });
        });
    </script>
</body>
</html>
"""

PROJECT_DETAIL_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ project.name }} - Kustify by KustBots</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        .pulse {
            animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
        }
    </style>
</head>
<body class="bg-gray-50">
    <!-- Navigation -->
    <nav class="bg-white shadow-lg">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-2xl font-bold gradient-text">Kustify</span>
                        <span class="ml-1 text-gray-600">by KustBots</span>
                    </div>
                    <div class="hidden md:ml-6 md:flex md:space-x-8">
                        <a href="/dashboard" class="border-indigo-500 text-gray-900 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Dashboard
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Projects
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Settings
                        </a>
                    </div>
                </div>
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-gray-700 mr-3">Welcome, {{ current_user.username }}</span>
                        <span class="text-sm bg-indigo-100 text-indigo-800 px-2 py-1 rounded-full">{{ current_user.plan }}</span>
                        <a href="/logout" class="ml-4 inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-all">
                            <i class="fas fa-sign-out-alt mr-1"></i> Logout
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div class="px-4 py-6 sm:px-0">
            <div class="flex justify-between items-center mb-6">
                <h1 class="text-2xl font-bold text-gray-900">{{ project.name }}</h1>
                <a href="/dashboard" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                    <i class="fas fa-arrow-left mr-2"></i> Back to Dashboard
                </a>
            </div>

            <div class="bg-white shadow overflow-hidden sm:rounded-lg mb-6">
                <div class="px-4 py-5 sm:px-6">
                    <h3 class="text-lg leading-6 font-medium text-gray-900">Project Information</h3>
                    <p class="mt-1 max-w-2xl text-sm text-gray-500">{{ project.description or 'No description' }}</p>
                </div>
                <div class="border-t border-gray-200">
                    <dl>
                        <div class="bg-gray-50 px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                            <dt class="text-sm font-medium text-gray-500">Type</dt>
                            <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2 capitalize">{{ project.template.replace('-', ' ') }}</dd>
                        </div>
                        <div class="bg-white px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                            <dt class="text-sm font-medium text-gray-500">Status</dt>
                            <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2">
                                {% if project.status == 'running' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">
                                    Running
                                </span>
                                {% elif project.status == 'deploying' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800 pulse">
                                    Deploying <i class="fas fa-spinner fa-spin ml-1"></i>
                                </span>
                                {% elif project.status == 'stopped' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">
                                    Stopped
                                </span>
                                {% elif project.status == 'error' %}
                                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800">
                                    Error
                                </span>
                                {% endif %}
                            </dd>
                        </div>
                        <div class="bg-gray-50 px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                            <dt class="text-sm font-medium text-gray-500">Port</dt>
                            <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2">{{ project.port }}</dd>
                        </div>
                        <div class="bg-white px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                            <dt class="text-sm font-medium text-gray-500">Created</dt>
                            <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2">{{ project.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</dd>
                        </div>
                        <div class="bg-gray-50 px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                            <dt class="text-sm font-medium text-gray-500">Last Updated</dt>
                            <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2">{{ project.updated_at.strftime('%Y-%m-%d %H:%M:%S') }}</dd>
                        </div>
                        {% if project.github_repo %}
                        <div class="bg-white px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                            <dt class="text-sm font-medium text-gray-500">GitHub Repository</dt>
                            <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2">
                                <a href="{{ project.github_repo }}" target="_blank" class="text-indigo-600 hover:text-indigo-900">
                                    {{ project.github_repo }}
                                </a>
                            </dd>
                        </div>
                        {% endif %}
                        {% if project.ngrok_tunnel %}
                        <div class="bg-gray-50 px-4 py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
                            <dt class="text-sm font-medium text-gray-500">Public URL</dt>
                            <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2">
                                <a href="{{ project.ngrok_tunnel.public_url }}" target="_blank" class="text-indigo-600 hover:text-indigo-900">
                                    {{ project.ngrok_tunnel.public_url }}
                                </a>
                            </dd>
                        </div>
                        {% endif %}
                    </dl>
                </div>
            </div>

            <div class="bg-white shadow overflow-hidden sm:rounded-lg mb-6">
                <div class="px-4 py-5 sm:px-6">
                    <h3 class="text-lg leading-6 font-medium text-gray-900">Actions</h3>
                </div>
                <div class="border-t border-gray-200 px-4 py-5 sm:p-6">
                    <div class="flex flex-wrap gap-3">
                        {% if project.status == 'running' %}
                        <form action="/project/{{ project.id }}/stop" method="post" class="inline">
                            <button type="submit" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-red-600 hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500">
                                <i class="fas fa-stop mr-2"></i> Stop
                            </button>
                        </form>
                        <form action="/project/{{ project.id }}/restart" method="post" class="inline">
                            <button type="submit" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-yellow-600 hover:bg-yellow-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-yellow-500">
                                <i class="fas fa-redo mr-2"></i> Restart
                            </button>
                        </form>
                        {% elif project.status == 'stopped' or project.status == 'error' %}
                        <form action="/project/{{ project.id }}/start" method="post" class="inline">
                            <button type="submit" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-green-600 hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-green-500">
                                <i class="fas fa-play mr-2"></i> Start
                            </button>
                        </form>
                        {% endif %}
                        
                        <a href="/project/{{ project.id }}/logs" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                            <i class="fas fa-file-alt mr-2"></i> View Logs
                        </a>
                        
                        {% if project.template == 'vps' %}
                        <a href="/project/{{ project.id }}/terminal" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                            <i class="fas fa-terminal mr-2"></i> Open Terminal
                        </a>
                        {% endif %}
                        
                        {% if project.template in ['web-service', 'static-site', 'github-docker', 'github-custom'] and project.status == 'running' %}
                        {% if project.ngrok_tunnel %}
                        <a href="{{ project.ngrok_tunnel.public_url }}" target="_blank" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                            <i class="fas fa-external-link-alt mr-2"></i> Open Application
                        </a>
                        {% else %}
                        <form action="/project/{{ project.id }}/ngrok/start" method="post" class="inline">
                            <button type="submit" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                                <i class="fas fa-link mr-2"></i> Create Public URL
                            </button>
                        </form>
                        {% endif %}
                        {% endif %}
                        
                        <form action="/project/{{ project.id }}/delete" method="post" class="inline" onsubmit="return confirm('Are you sure you want to delete this project?');">
                            <button type="submit" class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-red-600 hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500">
                                <i class="fas fa-trash mr-2"></i> Delete Project
                            </button>
                        </form>
                    </div>
                </div>
            </div>

            {% if project.template in ['web-service', 'static-site', 'github-docker', 'github-custom'] and project.status == 'running' %}
            <div class="bg-white shadow overflow-hidden sm:rounded-lg mb-6">
                <div class="px-4 py-5 sm:px-6">
                    <h3 class="text-lg leading-6 font-medium text-gray-900">Resource Usage</h3>
                </div>
                <div class="border-t border-gray-200 px-4 py-5 sm:p-6">
                    <div id="resource-stats" class="space-y-4">
                        <div>
                            <div class="flex justify-between mb-1">
                                <span class="text-sm font-medium text-gray-700">CPU Usage</span>
                                <span id="cpu-percent" class="text-sm font-medium text-gray-700">0%</span>
                            </div>
                            <div class="w-full bg-gray-200 rounded-full h-2.5">
                                <div id="cpu-bar" class="bg-indigo-600 h-2.5 rounded-full" style="width: 0%"></div>
                            </div>
                        </div>
                        <div>
                            <div class="flex justify-between mb-1">
                                <span class="text-sm font-medium text-gray-700">Memory Usage</span>
                                <span id="memory-mb" class="text-sm font-medium text-gray-700">0 MB</span>
                            </div>
                            <div class="w-full bg-gray-200 rounded-full h-2.5">
                                <div id="memory-bar" class="bg-indigo-600 h-2.5 rounded-full" style="width: 0%"></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            {% endif %}
        </div>
    </div>

    <script>
        {% if project.template in ['web-service', 'static-site', 'github-docker', 'github-custom'] and project.status == 'running' %}
        // Fetch resource stats
        function fetchResourceStats() {
            fetch('/project/{{ project.id }}/stats')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('cpu-percent').textContent = data.cpu_percent.toFixed(1) + '%';
                        document.getElementById('cpu-bar').style.width = data.cpu_percent + '%';
                        
                        document.getElementById('memory-mb').textContent = data.memory_mb.toFixed(1) + ' MB';
                        const memoryPercent = (data.memory_mb / {{ current_user.get_plan_limits().memory_limit }}) * 100;
                        document.getElementById('memory-bar').style.width = Math.min(memoryPercent, 100) + '%';
                    }
                })
                .catch(error => console.error('Error fetching resource stats:', error));
        }
        
        // Fetch stats every 5 seconds
        setInterval(fetchResourceStats, 5000);
        fetchResourceStats();
        {% endif %}
    </script>
</body>
</html>
"""

LOGS_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ project.name }} Logs - Kustify by KustBots</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
        .terminal {
            font-family: 'Courier New', Courier, monospace;
            background-color: #1e293b;
            color: #e2e8f0;
            padding: 1rem;
            border-radius: 0.375rem;
            height: 500px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .typing-effect {
            overflow: hidden;
            white-space: nowrap;
            animation: typing 2s steps(40, end);
        }
        @keyframes typing {
            from { width: 0 }
            to { width: 100% }
        }
    </style>
</head>
<body class="bg-gray-50">
    <!-- Navigation -->
    <nav class="bg-white shadow-lg">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-2xl font-bold gradient-text">Kustify</span>
                        <span class="ml-1 text-gray-600">by KustBots</span>
                    </div>
                    <div class="hidden md:ml-6 md:flex md:space-x-8">
                        <a href="/dashboard" class="border-indigo-500 text-gray-900 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Dashboard
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Projects
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Settings
                        </a>
                    </div>
                </div>
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-gray-700 mr-3">Welcome, {{ current_user.username }}</span>
                        <span class="text-sm bg-indigo-100 text-indigo-800 px-2 py-1 rounded-full">{{ current_user.plan }}</span>
                        <a href="/logout" class="ml-4 inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-all">
                            <i class="fas fa-sign-out-alt mr-1"></i> Logout
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div class="px-4 py-6 sm:px-0">
            <div class="flex justify-between items-center mb-6">
                <h1 class="text-2xl font-bold text-gray-900">{{ project.name }} Logs</h1>
                <div class="flex space-x-3">
                    <button id="refresh-logs" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                        <i class="fas fa-sync-alt mr-2"></i> Refresh
                    </button>
                    <a href="/dashboard" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                        <i class="fas fa-arrow-left mr-2"></i> Back to Dashboard
                    </a>
                </div>
            </div>

            <div class="bg-white shadow overflow-hidden sm:rounded-lg mb-6">
                <div class="px-4 py-5 sm:px-6">
                    <h3 class="text-lg leading-6 font-medium text-gray-900">Live Logs</h3>
                    <p class="mt-1 max-w-2xl text-sm text-gray-500">
                        {% if project.status == 'running' %}
                        Real-time logs from your running application
                        {% else %}
                        Logs from your application's last run
                        {% endif %}
                    </p>
                </div>
                <div class="border-t border-gray-200">
                    <div class="terminal" id="logs-container">
                        <div id="logs-content">Loading logs...</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const logsContent = document.getElementById('logs-content');
            const refreshButton = document.getElementById('refresh-logs');
            
            // Function to add logs to the container
            function addLog(message) {
                const logLine = document.createElement('div');
                logLine.textContent = message;
                logsContent.appendChild(logLine);
                
                // Auto-scroll to bottom
                const logsContainer = document.getElementById('logs-container');
                logsContainer.scrollTop = logsContainer.scrollHeight;
            }
            
            // Function to clear logs
            function clearLogs() {
                logsContent.innerHTML = '';
            }
            
            // Function to fetch logs
            function fetchLogs() {
                clearLogs();
                addLog('Fetching logs...');
                
                // Create WebSocket connection
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${protocol}//${window.location.host}/ws/logs/{{ project.id }}`;
                
                const socket = new WebSocket(wsUrl);
                
                socket.onopen = function(e) {
                    addLog('Connected to logs stream');
                };
                
                socket.onmessage = function(event) {
                    const data = JSON.parse(event.data);
                    
                    if (data.type === 'log') {
                        addLog(data.message);
                    } else if (data.type === 'error') {
                        addLog('Error: ' + data.message);
                    }
                };
                
                socket.onclose = function(event) {
                    if (event.wasClean) {
                        addLog(`Connection closed cleanly, code=${event.code} reason=${event.reason}`);
                    } else {
                        addLog('Connection died');
                    }
                };
                
                socket.onerror = function(error) {
                    addLog('Error: ' + error.message);
                };
                
                return socket;
            }
            
            // Initial fetch
            let socket = fetchLogs();
            
            // Refresh button
            refreshButton.addEventListener('click', function() {
                if (socket) {
                    socket.close();
                }
                socket = fetchLogs();
            });
        });
    </script>
</body>
</html>
"""

TERMINAL_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ project.name }} Terminal - Kustify by KustBots</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .gradient-text {
            background: linear-gradient(to right, #e73c7e, #23a6d5);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
        .terminal {
            font-family: 'Courier New', Courier, monospace;
            background-color: #1e293b;
            color: #e2e8f0;
            padding: 1rem;
            border-radius: 0.375rem;
            height: 500px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .terminal-input {
            font-family: 'Courier New', Courier, monospace;
            background-color: #1e293b;
            color: #e2e8f0;
            border: none;
            outline: none;
            width: 100%;
        }
        .terminal-line {
            display: flex;
        }
        .terminal-prompt {
            color: #10b981;
            margin-right: 0.5rem;
        }
        .terminal-cursor {
            display: inline-block;
            width: 0.5em;
            height: 1.2em;
            background-color: #e2e8f0;
            animation: blink 1s infinite;
        }
        @keyframes blink {
            0% { opacity: 1; }
            50% { opacity: 0; }
            100% { opacity: 1; }
        }
    </style>
</head>
<body class="bg-gray-50">
    <!-- Navigation -->
    <nav class="bg-white shadow-lg">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-2xl font-bold gradient-text">Kustify</span>
                        <span class="ml-1 text-gray-600">by KustBots</span>
                    </div>
                    <div class="hidden md:ml-6 md:flex md:space-x-8">
                        <a href="/dashboard" class="border-indigo-500 text-gray-900 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Dashboard
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Projects
                        </a>
                        <a href="#" class="border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium transition-all">
                            Settings
                        </a>
                    </div>
                </div>
                <div class="flex items-center">
                    <div class="flex-shrink-0 flex items-center">
                        <span class="text-gray-700 mr-3">Welcome, {{ current_user.username }}</span>
                        <span class="text-sm bg-indigo-100 text-indigo-800 px-2 py-1 rounded-full">{{ current_user.plan }}</span>
                        <a href="/logout" class="ml-4 inline-flex items-center px-3 py-2 border border-transparent text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-all">
                            <i class="fas fa-sign-out-alt mr-1"></i> Logout
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div class="px-4 py-6 sm:px-0">
            <div class="flex justify-between items-center mb-6">
                <h1 class="text-2xl font-bold text-gray-900">{{ project.name }} Terminal</h1>
                <div class="flex space-x-3">
                    <button id="clear-terminal" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                        <i class="fas fa-eraser mr-2"></i> Clear
                    </button>
                    <a href="/dashboard" class="inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50">
                        <i class="fas fa-arrow-left mr-2"></i> Back to Dashboard
                    </a>
                </div>
            </div>

            <div class="bg-white shadow overflow-hidden sm:rounded-lg mb-6">
                <div class="px-4 py-5 sm:px-6">
                    <h3 class="text-lg leading-6 font-medium text-gray-900">Web Terminal</h3>
                    <p class="mt-1 max-w-2xl text-sm text-gray-500">
                        Terminal access to your VPS container. You have root privileges.
                    </p>
                </div>
                <div class="border-t border-gray-200">
                    <div class="terminal" id="terminal-container">
                        <div id="terminal-content">
                            <div class="terminal-line">
                                <span class="terminal-prompt">kustify@vps:~$</span>
                                <span class="terminal-cursor"></span>
                            </div>
                        </div>
                        <div class="terminal-line">
                            <span class="terminal-prompt">kustify@vps:~$</span>
                            <input type="text" class="terminal-input" id="terminal-input" autofocus>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const terminalContent = document.getElementById('terminal-content');
            const terminalInput = document.getElementById('terminal-input');
            const clearButton = document.getElementById('clear-terminal');
            
            // Function to add output to the terminal
            function addOutput(output) {
                const outputLine = document.createElement('div');
                outputLine.textContent = output;
                terminalContent.appendChild(outputLine);
                
                // Auto-scroll to bottom
                const terminalContainer = document.getElementById('terminal-container');
                terminalContainer.scrollTop = terminalContainer.scrollHeight;
            }
            
            // Function to add a new command line
            function addCommandLine() {
                const commandLine = document.createElement('div');
                commandLine.className = 'terminal-line';
                commandLine.innerHTML = `
                    <span class="terminal-prompt">kustify@vps:~$</span>
                    <span class="terminal-cursor"></span>
                `;
                terminalContent.appendChild(commandLine);
                
                // Auto-scroll to bottom
                const terminalContainer = document.getElementById('terminal-container');
                terminalContainer.scrollTop = terminalContainer.scrollHeight;
            }
            
            // Function to clear the terminal
            function clearTerminal() {
                terminalContent.innerHTML = '';
                addCommandLine();
            }
            
            // Function to send command to the terminal
            function sendCommand(command) {
                // Create WebSocket connection
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${protocol}//${window.location.host}/ws/terminal/{{ project.id }}`;
                
                const socket = new WebSocket(wsUrl);
                
                socket.onopen = function(e) {
                    // Send the command
                    socket.send(JSON.stringify({
                        type: 'input',
                        data: command
                    }));
                };
                
                socket.onmessage = function(event) {
                    const data = JSON.parse(event.data);
                    
                    if (data.type === 'output') {
                        addOutput(data.data);
                    } else if (data.type === 'error') {
                        addOutput('Error: ' + data.message);
                    }
                };
                
                socket.onclose = function(event) {
                    if (event.wasClean) {
                        addOutput(`Connection closed cleanly, code=${event.code} reason=${event.reason}`);
                    } else {
                        addOutput('Connection died');
                    }
                    addCommandLine();
                };
                
                socket.onerror = function(error) {
                    addOutput('Error: ' + error.message);
                    addCommandLine();
                };
            }
            
            // Handle terminal input
            terminalInput.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    const command = terminalInput.value;
                    if (command.trim()) {
                        // Add the command to the terminal
                        const commandLine = document.createElement('div');
                        commandLine.className = 'terminal-line';
                        commandLine.innerHTML = `<span class="terminal-prompt">kustify@vps:~$</span> ${command}`;
                        terminalContent.appendChild(commandLine);
                        
                        // Clear the input
                        terminalInput.value = '';
                        
                        // Send the command
                        sendCommand(command);
                    }
                }
            });
            
            // Clear button
            clearButton.addEventListener('click', clearTerminal);
        });
    </script>
</body>
</html>
"""

# Routes
@app.route('/')
def index():
    return render_template_string(LANDING_PAGE)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password(user.password_hash, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_PAGE, error='Invalid username or password')
    
    return render_template_string(LOGIN_PAGE)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            return render_template_string(SIGNUP_PAGE, error='Passwords do not match')
        
        if User.query.filter_by(username=username).first():
            return render_template_string(SIGNUP_PAGE, error='Username already exists')
        
        if User.query.filter_by(email=email).first():
            return render_template_string(SIGNUP_PAGE, error='Email already exists')
        
        user = User(
            username=username,
            email=email,
            password_hash=hash_password(password)
        )
        
        db.session.add(user)
        db.session.commit()
        
        # Create user directory
        user_dir = os.path.join(Config.PROJECTS_ROOT, username)
        os.makedirs(user_dir, exist_ok=True)
        
        login_user(user)
        return redirect(url_for('dashboard'))
    
    return render_template_string(SIGNUP_PAGE)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    projects = Project.query.filter_by(user_id=current_user.id).all()
    plan_limits = current_user.get_plan_limits()
    return render_template_string(DASHBOARD_PAGE, projects=projects, plan_limits=plan_limits)

@app.route('/new-deployment', methods=['GET', 'POST'])
@login_required
def new_deployment():
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        template = request.form.get('template')
        config = {}
        github_repo = request.form.get('github_repo')
        
        # Check project limit
        plan_limits = current_user.get_plan_limits()
        if plan_limits['max_projects'] > 0:
            current_projects = Project.query.filter_by(user_id=current_user.id).count()
            if current_projects >= plan_limits['max_projects']:
                return render_template_string(NEW_DEPLOYMENT_PAGE, 
                                           error=f'Your {current_user.plan} plan allows only {plan_limits["max_projects"]} projects')
        
        # Template-specific configuration
        if template == 'pyrogram-bot':
            config['bot_token'] = request.form.get('bot_token')
            config['api_id'] = request.form.get('api_id')
            config['api_hash'] = request.form.get('api_hash')
        elif template == 'static-site':
            config['index_html'] = request.form.get('index_html')
        elif template == 'web-service':
            config['requirements'] = request.form.get('requirements')
            config['main_file'] = request.form.get('main_file')
        elif template == 'vps':
            config['os'] = request.form.get('os', 'ubuntu:22.04')
            config['packages'] = request.form.get('packages', '')
        elif template == 'github-docker':
            config['github_repo'] = github_repo
        elif template == 'github-custom':
            config['github_repo'] = github_repo
            config['build_command'] = request.form.get('build_command', '')
            config['start_command'] = request.form.get('start_command')
        
        # Get port
        port = int(request.form.get('port', 8000))
        
        # Create project
        project = Project(
            name=name,
            description=description,
            template=template,
            user_id=current_user.id,
            port=port,
            config=json.dumps(config),
            github_repo=github_repo
        )
        
        db.session.add(project)
        db.session.commit()
        
        # Create project directory
        project_dir = get_project_dir(project)
        os.makedirs(project_dir, exist_ok=True)
        
        # Set up project based on template
        setup_project(project, config)
        
        # If Ngrok token is provided, save it for later use
        ngrok_token = request.form.get('ngrok_token')
        if ngrok_token and template in ['web-service', 'static-site', 'github-docker', 'github-custom']:
            # Store the token in the config for later use
            config['ngrok_token'] = ngrok_token
            project.config = json.dumps(config)
            db.session.commit()
        
        return redirect(url_for('dashboard'))
    
    return render_template_string(NEW_DEPLOYMENT_PAGE)

@app.route('/project/<int:project_id>')
@login_required
def project_detail(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return redirect(url_for('dashboard'))
    
    return render_template_string(PROJECT_DETAIL_PAGE, project=project)

@app.route('/project/<int:project_id>/start', methods=['POST'])
@login_required
def start_project(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    if project.status == 'running':
        return jsonify({'success': False, 'message': 'Project is already running'}), 400
    
    try:
        # Set status to deploying
        project.status = 'deploying'
        db.session.commit()
        
        if docker_available:
            start_docker_deployment(project)
        else:
            start_native_deployment(project)
        
        return jsonify({'success': True, 'message': 'Project is starting'})
    except Exception as e:
        project.status = 'error'
        project.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.error(f"Error starting project {project.id}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/project/<int:project_id>/stop', methods=['POST'])
@login_required
def stop_project(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    if project.status != 'running':
        return jsonify({'success': False, 'message': 'Project is not running'}), 400
    
    try:
        if docker_available and project.container_id:
            stop_docker_deployment(project)
        else:
            stop_native_deployment(project)
        
        project.status = 'stopped'
        project.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Project stopped successfully'})
    except Exception as e:
        logger.error(f"Error stopping project {project.id}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/project/<int:project_id>/restart', methods=['POST'])
@login_required
def restart_project(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    try:
        if project.status == 'running':
            if docker_available and project.container_id:
                stop_docker_deployment(project)
            else:
                stop_native_deployment(project)
        
        # Set status to deploying
        project.status = 'deploying'
        db.session.commit()
        
        if docker_available:
            start_docker_deployment(project)
        else:
            start_native_deployment(project)
        
        return jsonify({'success': True, 'message': 'Project is restarting'})
    except Exception as e:
        project.status = 'error'
        project.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.error(f"Error restarting project {project.id}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/project/<int:project_id>/delete', methods=['POST'])
@login_required
def delete_project(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    try:
        if project.status == 'running':
            if docker_available and project.container_id:
                stop_docker_deployment(project)
            else:
                stop_native_deployment(project)
        
        # Remove project directory
        project_dir = get_project_dir(project)
        if os.path.exists(project_dir):
            shutil.rmtree(project_dir)
        
        # Stop Ngrok tunnel if exists
        if project.ngrok_tunnel:
            stop_ngrok_tunnel(project.ngrok_tunnel)
            db.session.delete(project.ngrok_tunnel)
        
        db.session.delete(project)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Project deleted successfully'})
    except Exception as e:
        logger.error(f"Error deleting project {project.id}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/project/<int:project_id>/logs')
@login_required
def project_logs(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return redirect(url_for('dashboard'))
    
    return render_template_string(LOGS_PAGE, project=project)

@app.route('/project/<int:project_id>/stats')
@login_required
def project_stats(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    if project.status != 'running':
        return jsonify({'success': False, 'message': 'Project is not running'}), 400
    
    try:
        if docker_available and project.container_id:
            # Get stats from Docker
            container = docker_client.containers.get(project.container_id)
            stats = container.stats(stream=False)
            
            # Calculate CPU usage
            cpu_percent = calculate_cpu_percent(stats)
            
            # Get memory usage
            memory_usage = stats['memory_stats']['usage'] / (1024 * 1024)  # MB
            
            return jsonify({
                'success': True,
                'cpu_percent': cpu_percent,
                'memory_mb': memory_usage,
                'status': project.status
            })
        else:
            # Fallback to process-based stats
            if not project.container_id:  # Native deployment
                return jsonify({'success': False, 'message': 'Native deployment stats not available'}), 400
            
            return jsonify({'success': False, 'message': 'Container not found'}), 404
    except Exception as e:
        logger.error(f"Error getting stats for project {project.id}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/project/<int:project_id>/terminal')
@login_required
def project_terminal(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return redirect(url_for('dashboard'))
    
    if project.template != 'vps':
        return redirect(url_for('project_detail', project_id=project_id))
    
    return render_template_string(TERMINAL_PAGE, project=project)

@app.route('/project/<int:project_id>/ngrok/start', methods=['POST'])
@login_required
def start_ngrok_tunnel(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    if project.status != 'running':
        return jsonify({'success': False, 'message': 'Project is not running'}), 400
    
    # Check if user has Ngrok tunnel quota
    plan_limits = current_user.get_plan_limits()
    active_tunnels = NgrokTunnel.query.filter_by(user_id=current_user.id, active=True).count()
    if active_tunnels >= plan_limits['max_ngrok_tunnels']:
        return jsonify({'success': False, 'message': f'Your {current_user.plan} plan allows only {plan_limits["max_ngrok_tunnels"]} Ngrok tunnels'}), 400
    
    if not ngrok_available:
        return jsonify({'success': False, 'message': 'Ngrok is not available'}), 500
    
    try:
        # Determine the port to expose
        if project.template == 'pyrogram-bot':
            # Bots don't typically expose ports, so we'll skip
            return jsonify({'success': False, 'message': 'Ngrok not supported for this template'}), 400
        
        local_port = project.port
        
        # Get Ngrok token from config if available
        config = json.loads(project.config)
        ngrok_token = config.get('ngrok_token')
        
        if ngrok_token:
            # Configure Ngrok with the provided token
            from pyngrok import ngrok, conf
            conf.set_default_auth_token(ngrok_token)
        
        # Wait a bit for the service to start
        time.sleep(10)
        
        # Start Ngrok tunnel
        tunnel = ngrok.connect(local_port, proto="http")
        
        # Save tunnel info
        ngrok_tunnel = NgrokTunnel(
            user_id=current_user.id,
            project_id=project.id,
            public_url=tunnel.public_url,
            local_port=local_port,
            proto="http",
            active=True
        )
        
        db.session.add(ngrok_tunnel)
        db.session.commit()
        
        # Update project with tunnel ID
        project.ngrok_tunnel_id = ngrok_tunnel.id
        db.session.commit()
        
        return jsonify({
            'success': True,
            'public_url': tunnel.public_url,
            'tunnel_id': ngrok_tunnel.id
        })
    except Exception as e:
        logger.error(f"Error starting Ngrok tunnel for project {project.id}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/project/<int:project_id>/ngrok/stop', methods=['POST'])
@login_required
def stop_ngrok_tunnel_for_project(project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    if not project.ngrok_tunnel:
        return jsonify({'success': False, 'message': 'No active Ngrok tunnel for this project'}), 400
    
    try:
        stop_ngrok_tunnel(project.ngrok_tunnel)
        db.session.delete(project.ngrok_tunnel)
        project.ngrok_tunnel_id = None
        db.session.commit()
        return jsonify({'success': True, 'message': 'Ngrok tunnel stopped successfully'})
    except Exception as e:
        logger.error(f"Error stopping Ngrok tunnel for project {project.id}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/selfcheck')
def selfcheck():
    try:
        # Check Python version
        python_version = sys.version
        
        # Check SQLite
        sqlite_ok = False
        try:
            conn = sqlite3.connect(':memory:')
            conn.execute('SELECT 1')
            conn.close()
            sqlite_ok = True
        except Exception:
            pass
        
        # Check Docker
        docker_ok = docker_available
        
        # Check Ngrok
        ngrok_ok = ngrok_available
        
        return jsonify({
            'status': 'ok',
            'python_version': python_version,
            'sqlite_ok': sqlite_ok,
            'docker_available': docker_ok,
            'ngrok_available': ngrok_ok,
            'platform': platform.system()
        })
    except Exception as e:
        logger.error(f"Selfcheck failed: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# WebSocket for logs
@sock.route('/ws/logs/<int:project_id>')
@login_required
def logs_ws(ws, project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        ws.close()
        return
    
    if project.status not in ['running', 'deploying']:
        ws.send(json.dumps({'type': 'error', 'message': 'Project is not running'}))
        ws.close()
        return
    
    try:
        if docker_available and project.container_id:
            # Stream logs from Docker container
            container = docker_client.containers.get(project.container_id)
            
            # First, send existing logs
            existing_logs = container.logs().decode('utf-8')
            if existing_logs:
                ws.send(json.dumps({'type': 'log', 'message': existing_logs}))
            
            # Then stream new logs
            for log in container.logs(stream=True, follow=True, tail=0):
                if log:
                    ws.send(json.dumps({'type': 'log', 'message': log.decode('utf-8')}))
                
                # Check if connection is still open
                if not ws.connected:
                    break
        else:
            # Fallback to file-based logs
            log_file = os.path.join(get_project_dir(project), 'logs', 'app.log')
            
            if not os.path.exists(log_file):
                ws.send(json.dumps({'type': 'error', 'message': 'Log file not found'}))
                ws.close()
                return
            
            with open(log_file, 'r') as f:
                # Go to end of file
                f.seek(0, 2)
                
                while True:
                    line = f.readline()
                    if line:
                        ws.send(json.dumps({'type': 'log', 'message': line}))
                    else:
                        time.sleep(0.1)
                        
                        # Check if connection is still open
                        if not ws.connected:
                            break
    except Exception as e:
        logger.error(f"Error streaming logs for project {project.id}: {str(e)}")
        ws.send(json.dumps({'type': 'error', 'message': str(e)}))
    finally:
        ws.close()

# WebSocket for terminal
@sock.route('/ws/terminal/<int:project_id>')
@login_required
def terminal_ws(ws, project_id):
    project = db.session.get(Project, project_id)
    
    if not project or project.user_id != current_user.id:
        ws.close()
        return
    
    if project.template != 'vps' or project.status != 'running':
        ws.send(json.dumps({'type': 'error', 'message': 'Terminal not available'}))
        ws.close()
        return
    
    if not docker_available or not project.container_id:
        ws.send(json.dumps({'type': 'error', 'message': 'Docker not available'}))
        ws.close()
        return
    
    try:
        container = docker_client.containers.get(project.container_id)
        
        # Create an exec instance for a shell
        exec_instance = container.exec_create(
            cmd="/bin/bash",
            stdin=True,
            tty=True,
            detach=False
        )
        
        # Start the exec instance
        socket = container.exec_start(exec_instance['Id'], socket=True)
        
        # Function to read from the socket and send to WebSocket
        def read_from_socket():
            try:
                while True:
                    data = socket._sock.recv(4096)
                    if not data:
                        break
                    ws.send(json.dumps({'type': 'output', 'data': data.decode('utf-8')}))
            except Exception as e:
                logger.error(f"Error reading from socket: {str(e)}")
                ws.send(json.dumps({'type': 'error', 'message': str(e)}))
        
        # Start a thread to read from the socket
        read_thread = threading.Thread(target=read_from_socket)
        read_thread.daemon = True
        read_thread.start()
        
        # Handle WebSocket messages
        while True:
            message = ws.receive()
            if message is None:
                break
            
            try:
                data = json.loads(message)
                if data.get('type') == 'input':
                    socket._sock.send(data.get('data', '').encode('utf-8'))
            except Exception as e:
                logger.error(f"Error handling WebSocket message: {str(e)}")
                ws.send(json.dumps({'type': 'error', 'message': str(e)}))
                
    except Exception as e:
        logger.error(f"Error setting up terminal for project {project.id}: {str(e)}")
        ws.send(json.dumps({'type': 'error', 'message': str(e)}))
    finally:
        ws.close()

# Helper functions
def get_project_dir(project):
    return os.path.join(Config.PROJECTS_ROOT, project.user.username, 'projects', str(project.id))

def setup_project(project, config):
    project_dir = get_project_dir(project)
    
    # Create logs directory
    logs_dir = os.path.join(project_dir, 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    
    # Set up based on template
    if project.template == 'pyrogram-bot':
        # Create bot.py file
        bot_file = os.path.join(project_dir, 'bot.py')
        with open(bot_file, 'w') as f:
            f.write(PYROGRAM_BOT_TEMPLATE.format(
                bot_token=config['bot_token'],
                api_id=config['api_id'],
                api_hash=config['api_hash']
            ))
        
        # Create requirements.txt
        requirements_file = os.path.join(project_dir, 'requirements.txt')
        with open(requirements_file, 'w') as f:
            f.write('pyrogram\n')
    
    elif project.template == 'static-site':
        # Create index.html
        index_file = os.path.join(project_dir, 'index.html')
        with open(index_file, 'w') as f:
            f.write(config.get('index_html', STATIC_SITE_TEMPLATE))
        
        # Create app.py for serving
        app_file = os.path.join(project_dir, 'app.py')
        with open(app_file, 'w') as f:
            f.write(STATIC_SITE_APP)
        
        # Create requirements.txt
        requirements_file = os.path.join(project_dir, 'requirements.txt')
        with open(requirements_file, 'w') as f:
            f.write('flask\n')
    
    elif project.template == 'web-service':
        # Create main.py
        main_file = os.path.join(project_dir, config.get('main_file', 'main.py'))
        with open(main_file, 'w') as f:
            f.write(WEB_SERVICE_TEMPLATE)
        
        # Create requirements.txt
        requirements_file = os.path.join(project_dir, 'requirements.txt')
        with open(requirements_file, 'w') as f:
            f.write(config.get('requirements', 'flask\n'))
    
    elif project.template == 'worker':
        # Create main.py
        main_file = os.path.join(project_dir, config.get('main_file', 'main.py'))
        with open(main_file, 'w') as f:
            f.write(WORKER_TEMPLATE)
        
        # Create requirements.txt
        requirements_file = os.path.join(project_dir, 'requirements.txt')
        with open(requirements_file, 'w') as f:
            f.write(config.get('requirements', ''))
    
    elif project.template == 'vps':
        # Create Dockerfile for VPS
        dockerfile = os.path.join(project_dir, 'Dockerfile')
        with open(dockerfile, 'w') as f:
            os_type = config.get('os', 'ubuntu:22.04')
            packages = config.get('packages', '')
            
            f.write(f"""FROM {os_type}

# Install necessary packages including terminal tools
RUN apt-get update && apt-get install -y \\
    sudo \\
    curl \\
    wget \\
    git \\
    vim \\
    nano \\
    htop \\
    {packages} \\
    && rm -rf /var/lib/apt/lists/*

# Create a user
RUN useradd -m -s /bin/bash kustify
RUN echo 'kustify ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER kustify
WORKDIR /home/kustify

# Install ttyd for web terminal access
USER root
RUN curl -fsSL https://github.com/tsl0922/ttyd/releases/download/1.7.2/ttyd.x86_64 -o /usr/local/bin/ttyd && \\
    chmod +x /usr/local/bin/ttyd

USER kustify
CMD ["/usr/local/bin/ttyd", "-p", "7681", "-W", "/bin/bash"]
""")
        
        # Create startup script
        startup_script = os.path.join(project_dir, 'startup.sh')
        with open(startup_script, 'w') as f:
            f.write("""#!/bin/bash
echo "VPS container started. You can now connect via the web terminal."
echo "To get root access, type: sudo su"
echo "To install packages, type: sudo apt-get update && sudo apt-get install <package>"
""")
    
    elif project.template == 'github-docker':
        # GitHub repo with Dockerfile will be cloned during deployment
        pass
    
    elif project.template == 'github-custom':
        # GitHub repo with custom commands will be cloned during deployment
        pass

def start_docker_deployment(project):
    project_dir = get_project_dir(project)
    config = json.loads(project.config)
    plan_limits = project.user.get_plan_limits()
    
    # Build Docker image
    image_tag = f"kustify-{project.id}"
    
    try:
        # For GitHub repos, clone the repository first
        if project.template in ['github-docker', 'github-custom']:
            github_repo = config.get('github_repo')
            if github_repo:
                try:
                    # Clone the repo
                    subprocess.run(['git', 'clone', github_repo, project_dir], check=True)
                except subprocess.CalledProcessError as e:
                    logger.error(f"Failed to clone GitHub repository for project {project.id}: {str(e)}")
                    raise Exception(f"Failed to clone GitHub repository: {str(e)}")
        
        # Build the image
        docker_client.images.build(path=project_dir, tag=image_tag)
    except Exception as e:
        logger.error(f"Error building Docker image for project {project.id}: {str(e)}")
        raise Exception(f"Failed to build Docker image: {str(e)}")
    
    # Create and start container
    try:
        # Set up port mapping
        port_mapping = {}
        if project.template in ['static-site', 'web-service', 'github-docker', 'github-custom']:
            port_mapping[f'{project.port}/tcp'] = project.port
        
        # For VPS template, also map the ttyd port
        if project.template == 'vps':
            port_mapping['7681/tcp'] = 7681
        
        # Set up resource limits
        mem_limit = f"{plan_limits['memory_limit']}m"
        cpu_quota = int(plan_limits['cpu_limit'] * 100000)  # Convert to microseconds
        
        # Create container
        container = docker_client.containers.create(
            image=image_tag,
            name=f"kustify-{project.id}",
            detach=True,
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            cpu_period=100000,
            ports=port_mapping,
            volumes={
                project_dir: {'bind': '/app', 'mode': 'rw'},
                os.path.join(project_dir, 'logs'): {'bind': '/app/logs', 'mode': 'rw'}
            }
        )
        
        # Start the container
        container.start()
        
        # Save container ID
        project.container_id = container.id
        
        # Start a thread to monitor the container and update status when ready
        monitor_thread = threading.Thread(
            target=monitor_container_health,
            args=(project, container)
        )
        monitor_thread.daemon = True
        monitor_thread.start()
    except Exception as e:
        logger.error(f"Error starting Docker container for project {project.id}: {str(e)}")
        raise Exception(f"Failed to start Docker container: {str(e)}")

def monitor_container_health(project, container):
    try:
        # Wait for container to be ready
        if project.template in ['static-site', 'web-service', 'github-docker', 'github-custom']:
            # For web services, wait for the port to be open
            import socket
            for _ in range(30):  # 30 seconds timeout
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    result = sock.connect_ex(('localhost', project.port))
                    sock.close()
                    if result == 0:
                        break
                except:
                    pass
                time.sleep(1)
        elif project.template == 'vps':
            # For VPS, wait for ttyd to be ready
            import socket
            for _ in range(30):  # 30 seconds timeout
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    result = sock.connect_ex(('localhost', 7681))
                    sock.close()
                    if result == 0:
                        break
                except:
                    pass
                time.sleep(1)
        elif project.template == 'pyrogram-bot':
            # For bots, wait 2 minutes
            time.sleep(120)
        elif project.template == 'worker':
            # For workers, wait 10 seconds
            time.sleep(10)
        
        # Update project status to running
        with app.app_context():
            project = db.session.get(Project, project.id)
            if project:
                project.status = 'running'
                project.updated_at = datetime.now(timezone.utc)
                db.session.commit()
                
                # If Ngrok token is provided, start Ngrok tunnel
                config = json.loads(project.config)
                ngrok_token = config.get('ngrok_token')
                if ngrok_token and project.template in ['web-service', 'static-site', 'github-docker', 'github-custom']:
                    try:
                        start_ngrok_for_project(project, ngrok_token)
                    except Exception as e:
                        logger.error(f"Error starting Ngrok tunnel for project {project.id}: {str(e)}")
    except Exception as e:
        logger.error(f"Error monitoring container health for project {project.id}: {str(e)}")
        with app.app_context():
            project = db.session.get(Project, project.id)
            if project:
                project.status = 'error'
                project.updated_at = datetime.now(timezone.utc)
                db.session.commit()

def start_ngrok_for_project(project, ngrok_token):
    try:
        # Configure Ngrok with the provided token
        from pyngrok import ngrok, conf
        conf.set_default_auth_token(ngrok_token)
        
        # Wait a bit for the service to start
        time.sleep(10)
        
        # Start Ngrok tunnel
        tunnel = ngrok.connect(project.port, proto="http")
        
        # Save tunnel info
        with app.app_context():
            ngrok_tunnel = NgrokTunnel(
                user_id=project.user_id,
                project_id=project.id,
                public_url=tunnel.public_url,
                local_port=project.port,
                proto="http",
                active=True
            )
            
            db.session.add(ngrok_tunnel)
            db.session.commit()
            
            # Update project with tunnel ID
            project.ngrok_tunnel_id = ngrok_tunnel.id
            db.session.commit()
            
            logger.info(f"Ngrok tunnel started for project {project.id}: {tunnel.public_url}")
    except Exception as e:
        logger.error(f"Error starting Ngrok tunnel for project {project.id}: {str(e)}")
        raise

def stop_docker_deployment(project):
    try:
        container = docker_client.containers.get(project.container_id)
        container.stop()
        container.remove()
        project.container_id = None
    except Exception as e:
        logger.error(f"Error stopping Docker container for project {project.id}: {str(e)}")
        raise Exception(f"Failed to stop Docker container: {str(e)}")

def start_native_deployment(project):
    # Fallback to native deployment if Docker is not available
    project_dir = get_project_dir(project)
    logs_dir = os.path.join(project_dir, 'logs')
    
    # Ensure logs directory exists
    os.makedirs(logs_dir, exist_ok=True)
    
    # Create log file
    log_file = os.path.join(logs_dir, 'app.log')
    with open(log_file, 'w') as f:
        f.write(f"Starting deployment at {datetime.now(timezone.utc)}\n")
    
    # For GitHub repos, clone the repository first
    config = json.loads(project.config)
    if project.template in ['github-docker', 'github-custom']:
        github_repo = config.get('github_repo')
        if github_repo:
            try:
                # Clone the repo
                with open(log_file, 'a') as f:
                    f.write(f"Cloning GitHub repository: {github_repo}\n")
                
                result = subprocess.run(
                    ['git', 'clone', github_repo, project_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=True
                )
                
                with open(log_file, 'a') as f:
                    f.write(result.stdout)
            except subprocess.CalledProcessError as e:
                with open(log_file, 'a') as f:
                    f.write(f"Error cloning GitHub repository: {e.output}\n")
                raise Exception(f"Failed to clone GitHub repository: {e.output}")
    
    # Install requirements if needed
    requirements_file = os.path.join(project_dir, 'requirements.txt')
    if os.path.exists(requirements_file):
        with open(log_file, 'a') as f:
            f.write("Installing requirements...\n")
        
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-r', requirements_file],
                cwd=project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=True
            )
            
            with open(log_file, 'a') as f:
                f.write(result.stdout)
        except subprocess.CalledProcessError as e:
            with open(log_file, 'a') as f:
                f.write(f"Error installing requirements: {e.output}\n")
            raise Exception(f"Failed to install requirements: {e.output}")
    
    # Run build command if needed
    if project.template == 'github-custom' and config.get('build_command'):
        build_command = config.get('build_command')
        with open(log_file, 'a') as f:
            f.write(f"Running build command: {build_command}\n")
        
        try:
            # Split the command into parts
            cmd_parts = build_command.split()
            result = subprocess.run(
                cmd_parts,
                cwd=project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=True
            )
            
            with open(log_file, 'a') as f:
                f.write(result.stdout)
        except subprocess.CalledProcessError as e:
            with open(log_file, 'a') as f:
                f.write(f"Error running build command: {e.output}\n")
            raise Exception(f"Failed to run build command: {e.output}")
    
    # Start the application based on template
    if project.template == 'pyrogram-bot':
        cmd = [sys.executable, 'bot.py']
    elif project.template == 'static-site':
        cmd = [sys.executable, 'app.py']
    elif project.template == 'web-service':
        cmd = [sys.executable, config.get('main_file', 'main.py')]
    elif project.template == 'worker':
        cmd = [sys.executable, config.get('main_file', 'main.py')]
    elif project.template == 'github-custom':
        start_command = config.get('start_command')
        cmd = start_command.split()
    else:
        raise Exception(f"Unknown template: {project.template}")
    
    # Set up environment
    env = os.environ.copy()
    env['PYTHONPATH'] = project_dir
    env['PORT'] = str(project.port)
    
    # Start the process
    with open(log_file, 'a') as f:
        f.write(f"Starting application with command: {' '.join(cmd)}\n")
    
    # Use Popen to start the process
    process = subprocess.Popen(
        cmd,
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env
    )
    
    # Save PID as container_id (for compatibility)
    project.container_id = str(process.pid)
    
    # Start a thread to monitor the process and update status when ready
    monitor_thread = threading.Thread(
        target=monitor_native_process,
        args=(process, project, log_file)
    )
    monitor_thread.daemon = True
    monitor_thread.start()

def monitor_native_process(process, project, log_file):
    try:
        # Capture logs
        for line in iter(process.stdout.readline, ''):
            if line:
                with open(log_file, 'a') as f:
                    f.write(line)
        
        # Wait for exit
        return_code = process.wait()
        with open(log_file, 'a') as f:
            f.write(f"\nProcess exited with code {return_code}\n")
        
        # Update project status
        with app.app_context():
            project = db.session.get(Project, project.id)
            if project:
                if return_code == 0:
                    project.status = 'running'
                else:
                    project.status = 'error'
                project.updated_at = datetime.now(timezone.utc)
                db.session.commit()
    except Exception as e:
        with open(log_file, 'a') as f:
            f.write(f"\nError monitoring process: {str(e)}\n")
        
        with app.app_context():
            project = db.session.get(Project, project.id)
            if project:
                project.status = 'error'
                project.updated_at = datetime.now(timezone.utc)
                db.session.commit()

def stop_native_deployment(project):
    if not project.container_id:
        return
    
    try:
        pid = int(project.container_id)
        
        # Try to terminate gracefully
        os.kill(pid, signal.SIGTERM)
        
        # Wait for process to terminate
        for _ in range(10):  # 10 seconds max
            try:
                os.kill(pid, 0)  # Check if process exists
                time.sleep(1)
            except ProcessLookupError:
                break
        
        # If still running, force kill
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        
        project.container_id = None
    except Exception as e:
        logger.error(f"Error stopping native process for project {project.id}: {str(e)}")
        raise Exception(f"Failed to stop native process: {str(e)}")

def stop_ngrok_tunnel(tunnel):
    try:
        ngrok.disconnect(tunnel.public_url)
        tunnel.active = False
    except Exception as e:
        logger.error(f"Error stopping Ngrok tunnel {tunnel.id}: {str(e)}")

def calculate_cpu_percent(stats):
    cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
    system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
    
    if system_delta > 0.0 and cpu_delta > 0.0:
        cpu_percent = (cpu_delta / system_delta) * len(stats['cpu_stats']['cpu_usage']['percpu_usage']) * 100
    else:
        cpu_percent = 0.0
    
    return cpu_percent

# Template strings
PYROGRAM_BOT_TEMPLATE = r"""
import asyncio
from pyrogram import Client, filters

app = Client("my_account", bot_token="{bot_token}", api_id={api_id}, api_hash="{api_hash}")

@app.on_message(filters.command("start", prefixes=["/", "!", "."]) & filters.private)
async def start_command(client, message):
    await message.reply_text("Hello! I'm a bot created with Kustify by KustBots.")

@app.on_message(filters.command("help", prefixes=["/", "!", "."]) & filters.private)
async def help_command(client, message):
    await message.reply_text("This is a help message. You can customize it as needed.")

print("Bot started!")
app.run()
"""

STATIC_SITE_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>My Static Site</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            color: #333;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            border-radius: 5px;
            box-shadow: 0 0 10px rgba(0,0,0,0.1);
        }
        h1 {
            color: #2c3e50;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Welcome to My Static Site</h1>
        <p>This is a static site deployed with Kustify by KustBots.</p>
        <p>You can customize this HTML to create your own website.</p>
    </div>
</body>
</html>
"""

STATIC_SITE_APP = r"""
from flask import Flask, send_from_directory
import os

app = Flask(__name__)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port={{ port }})
"""

WEB_SERVICE_TEMPLATE = r"""
from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route('/')
def index():
    return jsonify({
        "message": "Hello from my web service!",
        "status": "running",
        "platform": "Kustify by KustBots"
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port={{ port }})
"""

WORKER_TEMPLATE = r"""
import time
import os

def main():
    print("Worker started")
    while True:
        # Do some work here
        print("Working...")
        time.sleep(10)

if __name__ == '__main__':
    main()
"""

if __name__ == '__main__':
    try:
        # Authenticate ngrok
        ngrok.set_auth_token("32SRDtFhdpCQc5dRCq3uxt9gKJp_4ZhMRojZbcD1XXXx3VAA6")

        # Start ngrok tunnel
        ngrok_tunnel = ngrok.connect(5000)
        public_url = ngrok_tunnel.public_url
        logger.info(f" * ngrok tunnel available at {public_url}")
        print(f" * ngrok tunnel available at {public_url}")

    except Exception as e:
        logger.error(f"Failed to start ngrok tunnel: {str(e)}")

    # Start Flask app
    app.run(host='0.0.0.0', port=5000, debug=True)
