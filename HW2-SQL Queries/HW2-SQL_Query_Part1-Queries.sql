/*
SQL queries to populate a Sailors and Boats dataset
My responses to Part 1 begin on line 91 at the bottom
*/
create table sailors(
    sid int PRIMARY KEY,
    sname varchar(30),
    rating int,
    age int
);

create table reserves(
    sid int,
    bid int,
    day date,
	PRIMARY KEY (sid, bid, day)
);

create table boats(
    bid int PRIMARY KEY,
	bname char(20),
	color char(10),
	length int
);

insert into sailors values (22,'dusting',7,45);
insert into sailors values (29,'brutus',1,33);
insert into sailors values (31,'lubber',8,55);
insert into sailors values (32,'andy',8,25);
insert into sailors values (58,'rusty',10,35);
insert into sailors values (64,'horatio',7,16);
insert into sailors values (71,'zorba',10,35);
insert into sailors values (74,'horatio',9,25);
insert into sailors values (85,'art',3,25);
insert into sailors values (95,'bob',3,63);
insert into sailors values (23,'emilio',7,45);
insert into sailors values (24,'scruntus',1,33);
insert into sailors values (35,'figaro',8,55);
insert into sailors values (59,'stum',8,25);
insert into sailors values (60,'jit',10,35);
insert into sailors values (61,'ossola',7,16);
insert into sailors values (62,'shaun',10,35);
insert into sailors values (88,'dan',9,25);
insert into sailors values (89,'dye',3,25);
insert into sailors values (90,'vin',3,63);

insert into reserves values (23,104,'1998/10/10');
insert into reserves values (24,104,'1998/10/10');
insert into reserves values (35,104,'1998/8/10');
insert into reserves values (59,105,'1998/7/10');
insert into reserves values (23,105,'1998/11/10');
insert into reserves values (35,105,'1998/11/6');
insert into reserves values (59,106,'1998/11/12');
insert into reserves values (60,106,'1998/9/5');
insert into reserves values (60,106,'1998/9/8');
insert into reserves values (88,107,'1998/9/8');
insert into reserves values (89,108,'1998/10/10');
insert into reserves values (90,109,'1998/10/10');
insert into reserves values (89,109,'1998/8/10');
insert into reserves values (60,109,'1998/7/10');
insert into reserves values (59,109,'1998/11/10');
insert into reserves values (62,110,'1998/11/6');
insert into reserves values (88,110,'1998/11/12');
insert into reserves values (88,110,'1998/9/5');
insert into reserves values (88,111,'1998/9/8');
insert into reserves values (61,112,'1998/9/8');
insert into reserves values (22,101,'1998/10/10');
insert into reserves values (22,102,'1998/10/10');
insert into reserves values (22,103,'1998/8/10');
insert into reserves values (22,104,'1998/7/10');
insert into reserves values (31,102,'1998/11/10');
insert into reserves values (31,103,'1998/11/6');
insert into reserves values (31,104,'1998/11/12');
insert into reserves values (64,101,'1998/9/5');
insert into reserves values (64,102,'1998/9/8');
insert into reserves values (74,103,'1998/9/8');

insert into boats values (101,'Interlake','blue', 45);
insert into boats values (102,'Interlake','red', 45);
insert into boats values (103,'Clipper','green', 40);
insert into boats values (104,'Clipper','red', 40);
insert into boats values (105,'Marine','red', 35);
insert into boats values (106,'Marine','green', 35);
insert into boats values (107,'Marine','blue', 35);
insert into boats values (108,'Driftwood','red', 35);
insert into boats values (109,'Driftwood','blue', 35);
insert into boats values (110,'Klapser','red', 30);
insert into boats values (111,'Sooney','green', 28);
insert into boats values (112,'Sooney','red', 28);

/********************* PART 1 ***************************/

/* Question 1 ************************/
SELECT B.bid, B.bname, COUNT(R.bid) AS Num_Reservations
FROM boats B
JOIN reserves R on B.bid = R.bid
GROUP BY B.bid, B.bname
ORDER BY B.bid ASC;
/* I learned that there are 2 ways to do a join */
SELECT B.bid, B.bname, COUNT(R.bid) AS Num_Reservations
FROM boats B, reserves R
WHERE B.bid = R.bid
GROUP BY B.bid, B.bname
ORDER BY B.bid ASC;

/* QUestion 2 *****************************/
SELECT S.sname, S.sid
FROM sailors S
JOIN reserves R ON S.sid = R.sid
JOIN boats B ON R.bid = B.bid
WHERE B.color = 'red'
GROUP BY S.sname, S.sid
HAVING COUNT(DISTINCT B.bid) = (SELECT COUNT(B2.bid)
                                FROM boats B2
                                WHERE B2.color = 'red')
ORDER BY S.sid ASC;
/* QUestion 3 *********************************/
/*i ran this query first just to check number of sialors who've never reserved because 
i need to exclude those who've never reserved and wanted to manually error check*/
SELECT S.sname AS num_sailors_no_res
FROM  sailors S
WHERE S.sid NOT IN (
    SELECT R.sid
    FROM reserves R
)

/* below is real answer for question 3 */
WITH sailors_with_res AS (
    SELECT S.sid
    FROM sailors S 
    JOIN reserves R ON S.sid = R.sid
)
SELECT swr.sid
FROM sailors_with_res swr
WHERE NOT EXISTS (
    SELECT *
    FROM reserves R2
    JOIN boats B ON R2.bid = B.bid
    WHERE R2.sid = swr.sid AND B.color <> 'red'
)
GROUP BY swr.sid, swr.sname
ORDER BY swr.sid ASC;

/* Question 4 *****************************/

/* create sub-table contianing count of appearances for 
each boat ID, use max to find most then display that boat's info */

WITH count_subquery AS (
    SELECT COUNT(R.bid) AS res_count
    FROM  reserves R
    GROUP BY R.bid
)
SELECT B.bid, B.bname, COUNT(R.sid) AS reservation_count
FROM boats B
JOIN reserves R ON B.bid = R.bid
GROUP BY B.bid, B.bname
HAVING COUNT (R.bid) = (
    SELECT MAX(res_count)
    FROM count_subquery
)

/* idk if its better to split the queries like i did initially 
or if its better to just combine it into something thats a little 
harder to interpret IMO*/
SELECT B.bid, B.bname, COUNT(R.sid) AS reservation_count
FROM boats B
JOIN reserves R ON B.bid = R.bid
GROUP BY B.bid, B.bname
HAVING COUNT(R.sid) = (
    SELECT MAX(res_count)
    FROM (
            SELECT COUNT(R.sid) AS res_count
            FROM reserves R
            GROUP BY R.bid
            )
            AS count_subquery
)

/* Question 5 **********************/
SELECT S.sid, S.sname
FROM sailors S
WHERE NOT EXISTS (
    SELECT *
    FROM reserves R2
    JOIN boats B ON R2.bid = B.bid
    WHERE R2.sid = S.sid AND B.color = 'red'
)
ORDER BY S.sid ASC;

/* Question 6 ****************************************/
SELECT AVG (S.age)
FROM sailors S
WHERE S.rating = 10;

/* Question 7 **************************************/
SELECT S.sid, S.sname, S.rating, S.age
FROM sailors S
WHERE (S.rating, S.age) IN (
    SELECT S2.rating, MIN(S2.age)
    FROM sailors S2
    GROUP BY S2.rating
    )
ORDER BY S.rating ASC;
/* there are ties for the youngest sailors of each age 
so i'll just leave it bc it doesnt specify otherwise*/

/*Question 8 ***********************************/
/* It took me forever to find the correct syntax for this bruh and it still looks disgusting. this question
was buns, professor*/
WITH count_table AS (
    SELECT R.sid,
           R.bid,
           COUNT(*) AS reservation_count
    FROM reserves R
    GROUP BY R.sid, R.bid
), /* this first subquery produes a table showing how many times each sailor reserved each boat*/
max_table AS (
    SELECT bid,
           MAX(reservation_count) AS max_res
    FROM count_table
    GROUP BY bid
) /* this second subquery produces a table showing the max number of reserverations for each boat */
SELECT B.bid, B.bname, S.sid, S.sname, C.reservation_count
FROM max_table M
JOIN count_table C ON C.bid = M.bid AND C.reservation_count = max_res
JOIN sailors S on S.sid = C.sid
JOIN boats B ON B.bid = C.bid
ORDER BY B.bid ASC;
/* There has to be a more efficient way to do this >:( */


/*PART 2 IN THE ATTACHED PDF*/
