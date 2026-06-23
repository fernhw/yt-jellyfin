[META]
title=How To Use Black Jack
authors=Unknown
template=generic
status=?
owner=?
notes=?

[HERO]


[MAIN_SECTION]
Load Blackjack in an empty Unity scene. Blackjack is a separate scene from the game arena.
Connect the start nodes in Blackjack with linkers. These linkers are connected to dialog boxes, event boxes, and arena starting events.
You need to create the linkers as the content agnostic nature of the system means we load the data FROM blackjack TO the scene
Optimizations: We backload blackjack to the scene as a quick database, this prevents loading 2 scenes or compiling too much.
Upgradability: BlackJack will deliver the version of data as it is required by the scene. We are still upgrading and improving the tools.
Writer friendly: The writers aren't directly interacting with the scene.



USE DIRECTIONS: Pretend you're the director and you are giving directions, all Blackjack nodes can call on Blackjack directions. These directions are loaded on bootstrap and allow for one-to-one interactions with the arena. So if dialog or event content includes text in braces this text activates a given direction. Deceptively simple in the outset.
Directions rule the world: Taken from the director's directions, we think of them as canned director's orders, "Move the camera to the side!", "You two walk to the door!", telling changes to cameras, allow for dynamic cinematic shots, while the director can also direct objects to move out of the way, creating a limitless control of the game from the BlackJack editor.
Optimization: to avoid GC errors we load in the bootstrap all directions and run through search on action, this may seem slow but it avoids GC peaks and its not on frame to frame runtime.



The Blackjack-to-engine integration enables all parts of the game design, from changing cameras to removing a character or implementing full real-time cutscenes.
By following these steps, writers and designers can easily create dynamic and engaging stories that incorporate all player actions and create a truly immersive gaming experience.

[SECONDARY]
_Add more details here._

[REFERENCES]


