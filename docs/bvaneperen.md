## Repository and Codebase Organization

This buzzer project is a Jackbox style classroom tool. It consists of a hosts who will create games, courses, rosters, and then run these games. It also has players who can be apart of certian course rosters, or join games as guests and answer questions on the screen that the host is projecting. Finally there is an admin component that controls permissions of hosts, which courses hosts are in control of, and managing users. 

### How the repository is organized

- The application
  The main code lives in both the `backend/` and `frontend/` folders of this project. Our backend uses python FastAPI. Within our `backend/` we have an `app/` folder which contains our routers, websocket, services, models, schemas, and migrations. Our backend docker image builds from this folder. Our `frontend/` consists of 3 seperate React projects of `host/`, `player/`, and `admin/`. This is one app per user case, and a `shared/theme/` folder is used by all of them for consistent UI styling. 

- Running the app
  Composing up with our `docker-compose.yml` file will spin up a container for nginx, our backend, MySQL, and redis. This gives anybody developing the same dev stack for consistent development. Our env secrets are kept in a `.env` file. Our frontend is 3 seperate vite projects which are ran with an npm run dev command.

- Tests
  Our testing for this app is kept in `tests/` which consists of unit tests which don't interact with our real db. Integration tests which run against a running instance of our backend. Engine tests which plays a real game. Browser tests which uses playwright to check our UI. We also use ruff, and typescript checking when pushing code. 

- Content and tools
  we have 16 games in `sample_games` which we can take the json of an import it into our game for easy test creation. We also have test rosters we can import in `sample_rosts`, and `scripts/` for seeding this demo data, simulating players for games, and several other checks and automations for this app. 

- Process and records
  Finally we have a `docs/` folder which is used to keep track of our design details, so we have records of our implementation plans. We also keep track of our ai_logs in `ai_log/` for reference in this document. FInally we have our `.claude/` folder which holds my skills and rules for this project. 

#### Running it locally with Docker

- Our app runs in docker containers that we spin up using our `docker-compose` file. This file runs 4 services all on one network. Docker does work to configure port numbers so that they are able to communicate, which is something that I as a dev do not need to worry about. 

- These containers ensured that my partner and I were both in sync through the whole project, and had the same ports, as well as versions of things like MySQL and Redis so we didn't have to worry about supporting mutiple versions when writing our code. 

- Our backend depends on a healthy MySQL and Redis container in order to start which is specified in the `depends_on` that is specified in the docker compose. 

### How the major components relate

#### Backend

- Our backend has 2 different entry points in, but both end up in the `services/` layer which handles most of the logic. Our different entry points are socket.io which takes it's own traffic, and everyting else falls throught ot our REST FastAPI. All of our backend folders also have their own function. `schemas/` purpose is to validate the request bodies that come in. `common/` handles tools like dependencies, logging, exceptions and rate limiting. This allows for us to use these tools consistently and now rewrite access or dependency checks in every file. Finally our `models/` are used for easy communication with the db. 

#### Frontend

 - Our frontend consists of 3 different vite apps which all serve different purposes. Our Host project does things like project games, create and edit games, controling game flow, and maintaining, exporting, and dowloading rosters. Our player project is very simple and only allows players to join a game, and answer questions. Finally our Admin app is able to decide who is allowed to do what in both of our apps. Players can only join games if they are a part of a course roster, or allow guests is enabled. 

 - We need to have these 3 different apps since they all run on different devices, with much different configurations. For example our Admin app does not create sockets, and our player app needs libraries to scan a QR code. 

 - Each of our host and player projects have a `GameLayout.tsx` file which manages socket creation, and stores reports from the socket in a React context.

 - The only shared code in this frontend lives in the `shared/` folder, and this is because these are reusable themes that should be shared by these apps for consistently and to avoid redundant code.  

#### How the frontend and backend talk

- The method of communication will be determined based on the action that is being taken. For example if we are editing a game as a host, we use the rest API for a simple edit like this. If we are posting a new question to a classroom of people we will use the socket

- Every socket message has it's own chosen audience. These rooms can be created on a per player id level, a per room level or a host level. This ensures that data between players and hosts that shouldn't be shared is not. 

- Our frontend and backend communicated differently in prod and dev. IN dev we have Vite which passes requests we have to our backend. and in production nginx does this. 

#### The data layer: MySQL and Redis

- We have MySQL configured for long lasting permanent data that we wish to keep for future access, and we use Redis for in round memory for fast access, as well as a caching layer. When we write, our MySQL is always written before Redis. 

- Permissions for this game are stored as data in MySQL tables. These roles need to persist for security reasons and ensuring consistency of rosters and roles which is why they are stored here. Having them all in tables also makes the convention for retreival of these permissions consistent. 

### How data flows through a game round

  1. The first thing that happens to kick off a game is when the host presses the start button for a quiz, or next to indicate movement to the next question. The server keeps track of what question the game is on if we are mid game when hitting the next button. 

  2. The server will then open up the questioni, but checking that hte sender of the request is the host of the game, and then that we still have the game room in redis if this is not the first question. The countdown for the question will not run in the browswer, but is ran on the server. 

  2.1 The new question obtained from this request contains meta data about the question itself, like type, id, prompt, timelimit, and point value. This also has an answer data field which stays on the server. 

  3. `GameLayout.tsx` will then send a new_question event to the phones, which will route to a /question screen to the respective question. The host will get the question and the counter. 

  4. After this the player will answer, and when sending the answer to the server, the phone browser will mark it as pending until the server accepts it to ensure that we still have a record of the answer if the server drops it. 

  5. The server will recieve an answer and then run a `_handle_answer` which will proccess the users answer. This ensures that the player has not already answers, that MySQL has the updated answer, the answer question is still open, the question has the correct ID, and that the room session is still in progress. 

  6. After a valid answers is accepted the answer will be scored and values will be saved in our DB. `game_service.py` will compare the questiona dn our answers, and update the players session score in the DB depending on what they answered depending on the scoring type of the question. 

  7. After our players score is saved in our db, then we will update the redis key with the new score. 

  8. After this our socket.io will send new updates to the phones and host. The phone will move to a locked screen after receving this status, and the projector will update its counter. 

  9. When the next host_advance arrives and the phase it QUESTION, we will show results. The host will close the question and wait for answeres still being saved. Host gets scores and leaderbaord from MySQL, and the answer distribution from Redis. The next Phase is then RESULTS

  10. After this the next host_advance can take us to a new question, and the process repeats, or the game ends and the phase becomes COMPLETED. 

### The context files I wrote

    - I created the inital 24 files for this context hierarchy by running the context-hierarchy skill on each section of leaf nodes, revising these, and then building up all the way to the project level CLAUDE.md file. After writing these I had them reviewed in another session that found some issues in my first draft of files, and these were then revised. There were some little typos and misnamed functions as well as tricky to spot edge cases that were messed up on the first creation of these. Reading throught the overview of these files gave me a good understanding of sections of the codebase, and helped me get an overall better understanding of how this repo worked. I also ensured that in the rest of my sessions and features that the context-sync ran and these files stayed up to date. 

## Design Process (Elephant/Goldfish)

  - This process was very beneficial in implementing my T4 feature, as well as all the other features that I implemented in this codebase like T9, and T6.

  - The first thing that I did in this process was have Claude explain to me how the repo worked in places that I knew I would need to implement. For example the `docs/plans/t4-ui-restructuring` required a full explanaiton of the code base since it touched a lot of files. This was all before I even mentioned the design-discussion skill to hopefully get the correct context in and better understand the feature by myself

  - After this explanation I would give a full description of what I wanted to claude. I woulnd't give explicit implementation details of how I wanted code to be written, this was just a feature level description that we started with, and I hoped it was able to connect some dots from the previous explanation

  - My next step was to then go in depth on how we would implement each of these features. I would have Claude propose a couple of options for each, explain trade offs and then I would usually go a little deeper on one or two to fully feel confident in my choice for an implementaiotn.

  - After a lot of back and forth of this I would then have a completed design doc which I would run the goldfish test on. This would find contradictions, or plain missing details, and the previous conversation would continue. Finally I would have a new context implement this feature, or give me more feedback.

  - I thought that this process was extremely helpful for me to better understand how Claude was implementing my big features, and it allowed me to have control over how I wanted them implemented with extremem specification, while still saving the time of not having to write the code.

  - My biggest plan was `docs/plans/t4-ui-restructuring.md` as mentioned before. Other plans like `docs/plans/t4-test-coverage.md` invovled only testing the previous implementation, which required a little less context and planning. My `docs/plans/t9-ui-theming.md` was another more specific instance were I only needed to work with the frontend so the inital planning was not as large, but some of the details for this did get a little tricky. Finally I had `docs/plans/t6-brody-quiz-game` which involved a lot of discussion about exact question types I would like for a quiz, I go to design the exact specifics of the quiz I wanted while not having to worry about putting out perfect JSON. 

## AI Tools Used

### Claude Code

Each file in `ai_log/` is one prompt and its response; the 8 characters after the last `_` are the session ID, so I reference whole sessions as `ai_log/*_<session>.md`.

- The first session I used was for my AI-Log confirmation MR, session `24032703` (`ai_log/*_24032703.md`). A lot of this session was used to help set up this project, and to just get it running locally before I started any sort of development. It did a very good job giving me the tools and commands to install the correct versions, and get my project up and running. I did notice that when I asked for a little info on the tools or reasons behind, that the output was very long and much more than I needed or didn't really explain it in the way that clicked well for me. 

- I used several sessions for my creation of the context Hierarchy, the first session is going directory by directory and running /context-hierarchy so I could verify smaller units of work at a time in session `59101054` (`ai_log/*_59101054.md`). The next session in this MR was just explaining some of the parts of the codebase that I didn't fully understand in session `39d6abdd` (`ai_log/*_39d6abdd.md`). Finally I had a seperaate session go through and see if It could find any issues with the current context hierarchy that I had in session `ae558de1` (`ai_log/*_ae558de1.md`) where it was able to catch and fix some errosr

- My next MR was the implementation of T4 and my first attempt at the elephant goldfish model. Session `176da0a6` (`ai_log/*_176da0a6.md`) was used for my elephant phase. This is where I had Claude explain several systems to me, and then I started a design discusssion off of this. I noticed that it took time to explain things that I was unsure about before we started desisning which was nice. I also liked how it made it easy for me to understand what we had decided, and what things we still needed to implement to our design along the way. I then ran the golfish test in session `17b2aa28` (`ai_log/*_17b2aa28.md`) and made some revisions that the elephant missed, I then revised the spec, and reviewed in session `f49b1c0a` (`ai_log/*_f49b1c0a.md`) where some more clarification eneded up being needed. So implementeation started in session `ea6a959a` (`ai_log/*_ea6a959a.md`) where no more clarification was needed. 

- I had a very similar experience and pattern in my second T4 merge request for more test coverage. I started the elephant in session `93e31f15` (`ai_log/*_93e31f15.md`). ran the goldfish test in session `955c9819` (`ai_log/*_955c9819.md`) and implemented in session `745b975a` (`ai_log/*_745b975a.md`). This was pretty straightforward, but had Claude explain to me what these tests were, how they differed from normal unit tests, and what I needed to do to properly run them. I also ensured that it didn't fully go out of scope and start adding unneccecary tests like it initally wanted to in the elephant phase. 

- My T9 Elephant phase was in session `b727e65e` (`ai_log/*_b727e65e.md`) where I caught it making a fairly large mistake. The first is that the dark and light mode contrast was not being considered in the final spec even though that is something I said initally, all this took was a little redirection, but it would have been a big miss on my end. I then goldfish tested and revised in session `9916ac3f` (`ai_log/*_9916ac3f.md`) where I made a small revision and then implemented in session `425cac0e` (`ai_log/*_425cac0e.md`).

- My last MR was creating 2 test games for T6. This is where I had the most back and forth, since this was not a tradidtional coding spec, and I wanted to ensure that the games fit the chosen themes that I had so there was a lot more input that I had to give. This took a little bit extra overhead, but I got very good results in my spec this was in session `17436b35` (`ai_log/*_17436b35.md`). Finally I goldfish tested, and implemented after some revisions in session `2951e427` (`ai_log/*_2951e427.md`) where it actually tooks looks at my provided pictures and ensured they would fit the quiz game. 

## Best Practices Learned

- The biggest thing I learned in this project was managing context, and giving full context throught the Elephant/Goldfish model. This helped me find issues in my own thinking while also being able to verify that an implementation would accomplish the exact things that I want. It felt like I had a lot more control over what claude would be implementing.

- I also had to work with merge conflits in this project a good amount, and not all of them were trivial to resolve. A lot of the issues came when I made UI changes, and changing the CLAUDE.md context files along with these. The styling changes were fairly easy to resolve, but trying to figure out what parts to keep from each branch for the context was a bit trickier since it involved knowing exactly what was changed in both branches. 

- I also learned a good amount about some webdev concepts like using a token library for styling instead of just the normal inline tailwind options that were used. I learned there are a lot of libraries that support many of the accessibility requirements that were needed for this project. I also learned a big about web sockets and how these are used in tandem with a REST API in the backend. I also learned how Redis and MySQL work together and how to orchestrate reading and writing from them at the same time. 

## UI Theming Screenshots (T9)

- Before: [ui/before/](ui/before/)
- After, light theme: [ui/after/light/](ui/after/light/)
- After, dark theme: [ui/after/dark/](ui/after/dark/)
- Contrast and accessibility notes: [ui/accessibility.md](ui/accessibility.md)
