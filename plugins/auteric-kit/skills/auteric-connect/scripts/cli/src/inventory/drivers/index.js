import express from './express.js';
import fastify from './fastify.js';
import nextjs from './nextjs.js';
import fastapi from './fastapi.js';
import django from './django.js';
import goNethttp from './goNethttp.js';
import graphql from './graphql.js';
import openapi from './openapi.js';

// Driver contract:
//   detect(ctx) -> evidence[]   ({type, file, detail}; empty = driver absent)
//   analyze(evidence, ctx) -> { kindHint, language, framework, routes[],
//                               services[], auth[], persistence[], dependencies[] }
// ctx is the bounded walk result: { root, files: [{path, size, content}], stats }.
export const DRIVERS = [express, fastify, nextjs, fastapi, django, goNethttp, graphql, openapi];
